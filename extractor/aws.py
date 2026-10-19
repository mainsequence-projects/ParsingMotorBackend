"""Thin wrappers around Textract (QUERIES) and Bedrock Converse. Pay-per-call only."""
import json
import re
import threading

import boto3
from botocore.config import Config

from .config import REGION, BEDROCK_MODEL, BEDROCK_FALLBACK_MODELS
from . import cost as costmeter

_cfg = Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120)
_local = threading.local()


def _client(name):
    # Always Frankfurt. config.REGION already refuses other AWS_REGION values.
    if not hasattr(_local, name):
        setattr(_local, name, boto3.client(name, region_name=REGION, config=_cfg))
    return getattr(_local, name)


usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
_usage_lock = threading.Lock()


def textract_queries(png: bytes, queries: dict[str, str]) -> dict:
    """queries: alias -> question. Returns alias -> {value, confidence, bbox}."""
    if not queries:
        return {}
    costmeter.record_textract_page(1, note=f"queries={len(queries)}")
    items = list(queries.items())
    out = {}
    for i in range(0, len(items), 15):
        chunk = items[i:i + 15]
        r = _client("textract").analyze_document(
            Document={"Bytes": png}, FeatureTypes=["QUERIES"],
            QueriesConfig={"Queries": [{"Text": q, "Alias": a} for a, q in chunk]})
        blocks = {b["Id"]: b for b in r["Blocks"]}
        for b in r["Blocks"]:
            if b["BlockType"] != "QUERY":
                continue
            alias = b["Query"]["Alias"]
            for rel in b.get("Relationships", []):
                if rel["Type"] != "ANSWER":
                    continue
                for rid in rel["Ids"]:
                    a = blocks[rid]
                    conf = a.get("Confidence", 0) / 100
                    if alias not in out or conf > out[alias]["confidence"]:
                        out[alias] = {"value": a.get("Text"), "confidence": round(conf, 3),
                                      "bbox": a.get("Geometry", {}).get("BoundingBox")}
    return out


def _parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    end = max(text.rfind("}"), text.rfind("]")) + 1
    return json.loads(text[start:end])


def converse_json(prompt: str, images: list[bytes] | None = None, system: str | None = None,
                  max_tokens: int = 2000, model_id: str | None = None,
                  fallbacks: list[str] | None = None):
    content = [{"image": {"format": "png", "source": {"bytes": img}}} for img in (images or [])]
    content.append({"text": prompt})
    kwargs = dict(messages=[{"role": "user", "content": content}],
                  inferenceConfig={"temperature": 0, "maxTokens": max_tokens})
    if system:
        kwargs["system"] = [{"text": system}]
    models = [model_id or BEDROCK_MODEL] + list(
        fallbacks if fallbacks is not None else BEDROCK_FALLBACK_MODELS)
    # dedupe preserving order
    seen, ordered = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m); ordered.append(m)
    last = None
    for model in ordered:
        for _ in range(2):
            try:
                r = _client("bedrock-runtime").converse(modelId=model, **kwargs)
                inn = r["usage"]["inputTokens"]
                out = r["usage"]["outputTokens"]
                with _usage_lock:
                    usage["calls"] += 1
                    usage["input_tokens"] += inn
                    usage["output_tokens"] += out
                costmeter.record_bedrock(model, inn, out)
                return _parse_json(r["output"]["message"]["content"][0]["text"]), model
            except costmeter.BudgetExceeded:
                raise
            except (json.JSONDecodeError, ValueError) as e:
                last = e
            except Exception as e:
                last = e
                break
    raise RuntimeError(f"Bedrock failed on all models: {last}")
