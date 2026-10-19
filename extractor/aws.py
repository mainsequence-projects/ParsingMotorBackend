"""Thin wrappers around Textract (sync AnalyzeDocument QUERIES) and Bedrock Converse.

Pay-per-call only. Every call: budget precheck -> optional cache -> call -> cost record.
Each wrapper returns call metadata (model/api version, cache hit) for the audit trail.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

import boto3
from botocore.config import Config

from . import cache
from . import cost as costmeter
from .config import BEDROCK_FALLBACK_MODELS, BEDROCK_MODEL, REGION

_cfg = Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120)
_local = threading.local()
usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
_usage_lock = threading.Lock()


def _client(name):
    if not hasattr(_local, name):
        setattr(_local, name, boto3.client(name, region_name=REGION, config=_cfg))
    return getattr(_local, name)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def textract_api_info() -> dict:
    try:
        ver = _client("textract").meta.service_model.api_version
    except Exception:
        ver = "unknown"
    return {"service": "textract", "operation": "AnalyzeDocument", "mode": "sync",
            "feature_types": ["QUERIES"], "api_version": ver, "region": REGION}


def textract_queries(png: bytes, queries: dict[str, str]):
    """Returns (answers, meta). answers: alias -> {value, confidence, bbox}."""
    if not queries:
        return {}, {"cache_hit": None, "called": False}
    items = sorted(queries.items())
    k = cache.key("textract.analyze_document.queries", img=sha256(png), queries=items)
    hit = cache.get(k)
    if hit is not None:
        costmeter.record_cache_hit("textract")
        answers, lines = (hit["answers"], hit.get("lines")) if hit.get("v") == 2 else (hit, None)
        return answers, {"cache_hit": True, "called": False, "image_sha256": sha256(png), "lines": lines}
    pages_billed = (len(items) + 14) // 15
    costmeter.precheck(costmeter.estimate_textract(pages_billed))
    out, lines = {}, None
    for i in range(0, len(items), 15):
        chunk = items[i:i + 15]
        r = _client("textract").analyze_document(
            Document={"Bytes": png}, FeatureTypes=["QUERIES"],
            QueriesConfig={"Queries": [{"Text": q, "Alias": a} for a, q in chunk]})
        costmeter.record_textract_page(1, note=f"queries={len(chunk)}")
        blocks = {b["Id"]: b for b in r["Blocks"]}
        if lines is None:   # same page for every chunk: keep the OCR lines once (reading order)
            lines = [{"text": b.get("Text", ""), "confidence": round(b.get("Confidence", 0) / 100, 3)}
                     for b in r["Blocks"] if b["BlockType"] == "LINE"]
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
    cache.put(k, {"v": 2, "answers": out, "lines": lines})
    return out, {"cache_hit": False, "called": True, "image_sha256": sha256(png), "lines": lines}


BEDROCK_MAX_IMAGE_BYTES = 3_500_000   # Converse rejects images > 3.75 MB


def _image_block(png: bytes) -> dict:
    """PNG as-is; a big photo-like PNG is re-encoded as JPEG so Bedrock accepts it."""
    if len(png) <= BEDROCK_MAX_IMAGE_BYTES:
        return {"image": {"format": "png", "source": {"bytes": png}}}
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buf, "JPEG", quality=85)
    return {"image": {"format": "jpeg", "source": {"bytes": buf.getvalue()}}}


def _parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    end = max(text.rfind("}"), text.rfind("]")) + 1
    return json.loads(text[start:end])


def converse_json(prompt: str, images: list[bytes] | None = None, system: str | None = None,
                  max_tokens: int = 2000, model_id: str | None = None,
                  fallbacks: list[str] | None = None, return_meta: bool = False):
    images = images or []
    models = []
    for m in [model_id or BEDROCK_MODEL] + list(fallbacks if fallbacks is not None
                                                 else BEDROCK_FALLBACK_MODELS):
        if m and m not in models:
            models.append(m)
    k = cache.key("bedrock.converse", models=models, prompt=prompt, system=system,
                  max_tokens=max_tokens, images=[sha256(i) for i in images])
    hit = cache.get(k)
    if hit is not None:
        costmeter.record_cache_hit("bedrock")
        meta = {"model": hit["model"], "cache_hit": True, "usage": hit.get("usage")}
        return (hit["data"], hit["model"], meta) if return_meta else (hit["data"], hit["model"])

    content = [_image_block(img) for img in images]
    content.append({"text": prompt})
    kwargs = dict(messages=[{"role": "user", "content": content}],
                  inferenceConfig={"temperature": 0, "maxTokens": max_tokens})
    if system:
        kwargs["system"] = [{"text": system}]
    # rough precheck: ~1.6k tokens/image + prompt, priced as primary model
    inn_p, out_p = costmeter._price_for(models[0])
    costmeter.precheck((1600 * len(images) + len(prompt) / 3) / 1e6 * inn_p + max_tokens / 1e6 * out_p)
    last = None
    for model in models:
        for _ in range(2):
            try:
                r = _client("bedrock-runtime").converse(modelId=model, **kwargs)
                inn, out = r["usage"]["inputTokens"], r["usage"]["outputTokens"]
                with _usage_lock:
                    usage["calls"] += 1
                    usage["input_tokens"] += inn
                    usage["output_tokens"] += out
                costmeter.record_bedrock(model, inn, out)
                data = _parse_json(r["output"]["message"]["content"][0]["text"])
                u = {"input_tokens": inn, "output_tokens": out}
                cache.put(k, {"data": data, "model": model, "usage": u})
                meta = {"model": model, "cache_hit": False, "usage": u}
                return (data, model, meta) if return_meta else (data, model)
            except costmeter.BudgetExceeded:
                raise
            except (json.JSONDecodeError, ValueError) as e:
                last = e
            except Exception as e:
                last = e
                break
    raise RuntimeError(f"Bedrock failed on all models: {last}")
