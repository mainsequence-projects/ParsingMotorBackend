"""Deterministic second reader for the Contract's list of required documents.

Input: Textract LINE blocks of the contract (raw, stored in the audit). Config: section
headers and synonyms in rules.yaml (COMPLETENESS.params.list_parser), so the bank can add
"potvrzení o příjmu" without touching code. Unknown bullet items become 'unknown:<slug>'
so they can never silently disappear (the LLM is told to map to known codes only).
"""
from __future__ import annotations

import re

from .normalize import strip_diacritics

BULLET = re.compile(r"^\s*(?:[-–•*·▪]|\d{1,2}[.)]|[a-z][.)])\s+")
DEFAULT_PARSER = {
    "section_headers": ["required documents"],
    "synonyms": {},
}


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", strip_diacritics(str(s)).lower()).strip()


def parse_required_documents(lines, params: dict | None) -> dict | None:
    """-> {"value": [codes], "confidence": min line conf, "page": header page} or None."""
    if not lines:
        return None
    p = {**DEFAULT_PARSER, **(params or {})}
    headers = [_plain(h) for h in p["section_headers"]]
    syn = {code: [_plain(x) for x in words] for code, words in (p["synonyms"] or {}).items()}
    start = next((i for i, ln in enumerate(lines)
                  if any(h in _plain(ln.get("text", "")) for h in headers)), None)
    if start is None:
        return None
    codes, confs = [], [lines[start].get("confidence", 0.0)]
    for ln in lines[start + 1:]:
        text = ln.get("text", "")
        if not BULLET.match(text):
            if codes:
                break          # first non-bullet line after the list ends it
            continue           # tolerate a blank/sub-title line right after the header
        item = _plain(BULLET.sub("", text))
        hit = [c for c, words in syn.items() if any(w and w in item for w in words)]
        codes += hit or ["unknown:" + re.sub(r"[^a-z0-9]+", "_", item).strip("_")[:40]]
        confs.append(ln.get("confidence", 0.0))
    if not codes:
        return None
    return {"value": sorted(set(codes)), "confidence": round(min(confs), 3),
            "page": lines[start].get("page")}
