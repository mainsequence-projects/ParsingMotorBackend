"""Optional content-addressed cache of AWS responses (opt-in: EXTRACTOR_CACHE=1).

Key = sha256 of (call kind, model/api, prompt/queries, sha256 of each image). Calls are
temperature 0, so an identical page image gives the same answer; we bill it once.
WARNING: responses contain document data. Only enable for synthetic/dev data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def enabled() -> bool:
    return os.getenv("EXTRACTOR_CACHE", "0") == "1"


def _dir() -> Path:
    d = Path(os.getenv("EXTRACTOR_CACHE_DIR") or Path(__file__).resolve().parents[1] / ".cache" / "aws")
    d.mkdir(parents=True, exist_ok=True)
    return d


def key(kind: str, **parts) -> str:
    blob = json.dumps({"kind": kind, **parts}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def get(k: str):
    if not enabled():
        return None
    p = _dir() / f"{k}.json"
    if p.exists():
        return json.loads(p.read_text())
    return None


def put(k: str, value) -> None:
    if not enabled():
        return
    p = _dir() / f"{k}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, default=str))
    tmp.replace(p)
