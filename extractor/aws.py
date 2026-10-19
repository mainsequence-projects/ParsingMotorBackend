"""Thin wrappers around Textract (QUERIES) and Bedrock Converse."""
import json
import re
import threading

import boto3
from botocore.config import Config

from .config import REGION, BEDROCK_MODEL, BEDROCK_FALLBACK_MODELS

_cfg = Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120)
_local = threading.local()


def _client(name):
    if not hasattr(_local, name):
        setattr(_local, name, boto3.client(name, region_name=REGION, config=_cfg))
    return getattr(_local, name)


# ---------------- Textract ----------------
def textract_queries(png: bytes, queries: dict[str, str]) -> dict:
    """queries: alias -> question. Returns alias -> {value, confidence} (max 15 queries per call)."""
    if not queries:
        return {}
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


# ---------------- Bedrock ----------------
usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
_usage_lock = threading.Lock()


def _parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    end = max(text.rfind("}"), text.rfind("]")) + 1
    return json.loads(text[start:end])


def converse_json(prompt: str, images: list[bytes] | None = None, system: str | None = None,
                  max_tokens: int = 2000):
    content = [{"image": {"format": "png", "source": {"bytes": img}}} for img in (images or [])]
    content.append({"text": prompt})
    kwargs = dict(messages=[{"role": "user", "content": content}],
                  inferenceConfig={"temperature": 0, "maxTokens": max_tokens})
    if system:
        kwargs["system"] = [{"text": system}]
    last = None
    for model in [BEDROCK_MODEL] + BEDROCK_FALLBACK_MODELS:
        for _ in range(2):
            try:
                r = _client("bedrock-runtime").converse(modelId=model, **kwargs)
                with _usage_lock:
                    usage["calls"] += 1
                    usage["input_tokens"] += r["usage"]["inputTokens"]
                    usage["output_tokens"] += r["usage"]["outputTokens"]
                return _parse_json(r["output"]["message"]["content"][0]["text"]), model
            except (json.JSONDecodeError, ValueError) as e:
                last = e  # retry once on malformed JSON
            except Exception as e:  # throttling / unavailable -> next model
                last = e
                break
    raise RuntimeError(f"Bedrock failed on all models: {last}")
