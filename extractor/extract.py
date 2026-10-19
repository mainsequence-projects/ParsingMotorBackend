"""Dual extraction: Textract QUERIES + Bedrock Converse, run in parallel. Raw outputs only."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from .aws import converse_json, textract_queries
from .config import MAX_WORKERS, SCHEMAS

SYSTEM = (
    "You extract structured fields from bank KYC / loan documents. "
    "Return ONLY JSON. Use null when a field is not present. "
    "Never invent values. Never convert or recalculate amounts — copy the printed figure."
)
MAX_BEDROCK_PAGES = 6  # cost guard
# codes the contract can require (every known type except the contract itself and 'other')
_DOC_CODES = tuple(t for t in SCHEMAS if t not in ("contract", "other"))


def _field_defs(doc_type: str) -> dict:
    return SCHEMAS.get(doc_type, {}).get("fields") or {}


def _textract_for_doc(doc: dict):
    fields = _field_defs(doc["doc_type"])
    queries = {k: f["query"] for k, f in fields.items() if f.get("textract", True) and f.get("query")}
    if not queries:
        return {}, []
    keep_lines = any(f.get("textract") == "lines" for f in fields.values())
    best, calls, lines_by_page = {}, [], {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(doc["pages"]))) as pool:
        futs = {pool.submit(textract_queries, p["png"], queries): p["page"] for p in doc["pages"]}
        for fut in as_completed(futs):
            page = futs[fut]
            answers, meta = fut.result()
            lines_by_page[page] = meta.pop("lines", None)
            calls.append({"page": page, **meta})
            for alias, ans in answers.items():
                if alias not in best or ans["confidence"] > best[alias]["confidence"]:
                    best[alias] = {**ans, "page": page}
    if keep_lines:
        # Raw OCR lines in document order: input for the deterministic list parser (rules layer).
        order = [p["page"] for p in doc["pages"]]
        if all(lines_by_page.get(pg) is not None for pg in order):
            best["_lines"] = [{**ln, "page": pg} for pg in order for ln in lines_by_page[pg]]
    return best, sorted(calls, key=lambda c: c["page"])


def _bedrock_for_doc(doc: dict):
    fields = _field_defs(doc["doc_type"])
    if not fields:
        return {}, None
    pages = doc["pages"][:MAX_BEDROCK_PAGES]
    hint = {}
    for name, meta in fields.items():
        if meta.get("type") == "doc_list":
            v = list(_DOC_CODES)
        elif meta.get("type") == "amount":
            v = 0.0
        else:
            v = "string or null"
        hint[name] = {"value": v, "image": 1}
    prompt = (
        f"This document is of type '{doc['doc_type']}' and has {len(pages)} image(s), numbered 1..{len(pages)} "
        "in the order given. Extract the fields below.\n"
        "Return ONLY a JSON object with exactly these keys; each value is an object "
        '{"value": <value or null>, "image": <1-based image number where you read it, or null>}:\n'
        f"{json.dumps(hint, indent=2)}\n"
        "Rules:\n"
        "- Dates as YYYY-MM-DD when possible, else keep the original string.\n"
        "- Amounts as numbers (no currency symbol). For declared_monthly_income use ONLY the figure "
        "next to that label, never the loan amount.\n"
        "- birth_number (rodne cislo) digits with optional slash, exactly as printed.\n"
        "- Copy names exactly as printed, including typos. Do not correct spelling.\n"
        f"- For required_documents, map free text to the codes: {', '.join(_DOC_CODES)}. "
        "Only include those listed as required.\n"
        "- Do not invent. Missing => null (or [] for lists)."
    )
    data, model, meta = converse_json(prompt, images=[p["png"] for p in pages], system=SYSTEM,
                                      max_tokens=1800, return_meta=True)
    out = {}
    if not isinstance(data, dict):
        data = {}
    for k in fields:
        v = data.get(k)
        img = None
        if isinstance(v, dict) and "value" in v:
            img, v = v.get("image"), v.get("value")
        try:
            page = pages[int(img) - 1]["page"] if img else None
        except (ValueError, IndexError, TypeError):
            page = None
        out[k] = {"value": v, "page": page, "model": model}
    return out, {"model": model, **meta}


def extract_document(doc: dict) -> dict:
    """Returns {textract, bedrock, calls} for one document group."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        ft = pool.submit(_textract_for_doc, doc)
        fb = pool.submit(_bedrock_for_doc, doc)
        t, tcalls = ft.result()
        b, bmeta = fb.result()
    return {"textract": t, "bedrock": b, "calls": {"textract": tcalls, "bedrock": bmeta}}
