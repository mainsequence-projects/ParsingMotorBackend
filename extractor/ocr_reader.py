"""Textract as the independent second reader for V2 extraction. Reading facts only.

Queries are generated from the Back Office schema titles, so every canonical document type
works without per-type configuration. Candidates are compared in a normalized form (Textract
has no Czech model and drops diacritics), and code, never a model, picks the draft value.
Acceptance, thresholds and cross-document rules stay in Back Office.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import aws
from .normalize import norm_amount, norm_date, norm_id, norm_text, strip_diacritics
from .schema_contract import validator

READER_ID = "ocr_textract"
MODEL = "textract:AnalyzeDocument/QUERIES"
NORMALIZATION_VERSION = "schema_ocr_vision_v2"
# OCR replaces the vision value only for values it reads natively, and only when Textract is
# confident; text keeps the vision reading (OCR loses Czech diacritics).
OCR_WINS_MIN_CONF = 0.9
OCR_WINS_KINDS = {"amount", "date", "id"}
CURRENCY = {"KČ": "CZK", "KC": "CZK", "K": "CZK", "€": "EUR", "$": "USD"}
ID_KEY = re.compile(r"(identifier|number|account|iban)", re.I)
SCALARS = {"string", "number", "integer"}


def _resolve(root: dict, s: dict) -> dict:
    """Follow local $refs and drop the nullable branch of anyOf."""
    for _ in range(20):
        if "$ref" in s:
            s = root.get("$defs", {}).get(s["$ref"].rsplit("/", 1)[-1], {})
            continue
        branches = [b for b in s.get("anyOf", []) if b.get("type") != "null"]
        if len(branches) == 1 and "anyOf" in s:
            s = {**{k: v for k, v in s.items() if k != "anyOf"}, **branches[0]}
            continue
        if branches:
            return {**s, "anyOf": branches}
        return s
    return s


def _walk(root: dict, pointer: str):
    s, titles = _resolve(root, root), []
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        prop = (s.get("properties") or {}).get(part)
        if prop is None:
            return None, titles
        s = _resolve(root, prop)
        titles.append(prop.get("title") or s.get("title") or part.replace("_", " "))
    return s, titles


def _types(leaf: dict) -> set:
    t = leaf.get("type")
    out = set(t) if isinstance(t, list) else {t} if t else set()
    for b in leaf.get("anyOf", []):
        out |= _types(b)
    return out - {None, "null"}


def leaf_schema(root: dict, pointer: str) -> dict | None:
    """Schema of a scalar leaf, or None for arrays items, objects and unknown pointers."""
    s, _ = _walk(root, pointer)
    return s if s is not None and _types(s) and _types(s) <= SCALARS else None


def query_text(root: dict, pointer: str) -> str | None:
    s, titles = _walk(root, pointer)
    if s is None or not _types(s) or not _types(s) <= SCALARS:
        return None
    words = []
    for t in titles:      # 'Opening balance' + 'Amount' -> 'opening balance amount'
        if t.lower() not in " ".join(words).lower():
            words.append(t)
    return f"What is the {' '.join(words).lower()}?"[:200]


def kind(leaf: dict | None, pointer: str) -> str:
    if leaf is None:
        return "other"
    types = _types(leaf)
    if leaf.get("format") == "date":
        return "date"
    if types & {"number", "integer"}:
        return "amount"
    if leaf.get("pattern") == "^[A-Z]{3}$":
        return "currency"
    if ID_KEY.search(pointer.rsplit("/", 1)[-1]):
        return "id"
    return "text"


def compare_form(leaf: dict | None, pointer: str, v):
    """Normalized candidate value: equal forms mean the readers agree."""
    if v is None or isinstance(v, (dict, list, bool)):
        return v
    k = kind(leaf, pointer)
    if k == "amount":
        x = float(v) if isinstance(v, (int, float)) else norm_amount(v)
        return f"{x:.2f}" if x is not None else str(v)
    if k == "date":
        return norm_date(v) or str(v)
    if k == "currency":
        s = str(v).strip().upper()
        return CURRENCY.get(s, CURRENCY.get(strip_diacritics(s), s))
    if k == "id":
        return norm_id(v) or str(v)
    return norm_text(v) if isinstance(v, str) else v


def draft_value(leaf: dict, pointer: str, raw: str):
    """OCR reading in the shape the draft schema expects, or None if it does not fit."""
    k = kind(leaf, pointer)
    if k == "amount":
        x = norm_amount(raw)
        value = None if x is None else f"{x:.2f}" if "string" in _types(leaf) else x
    elif k == "date":
        value = norm_date(raw)
    else:
        value = str(raw).strip() or None
    return value if value is not None and validator(leaf).is_valid(value) else None


def candidate_value(leaf: dict, pointer: str, vision_value, raw):
    """OCR candidate in draft shape. Same normalized reading as the vision reader -> the vision
    spelling (OCR dropped diacritics or formatting); raw_text keeps what Textract printed."""
    if raw is None:
        return None
    if vision_value is not None and compare_form(leaf, pointer, raw) == compare_form(leaf, pointer, vision_value):
        return vision_value
    value = draft_value(leaf, pointer, raw)
    return value if value is not None else str(raw)


def box(bbox: dict | None) -> list[float] | None:
    if not bbox:
        return None
    left, top = max(0.0, bbox["Left"]), max(0.0, bbox["Top"])
    right, bottom = min(1.0, left + bbox["Width"]), min(1.0, top + bbox["Height"])
    return [left, top, right, bottom] if right > left and bottom > top else None


def read(pages, root: dict, values: dict, citations: dict) -> dict:
    """pointer -> best Textract answer {value, confidence, bbox, page, source_file}."""
    by_page = defaultdict(dict)
    every_page = [p.page for p in pages]
    for pointer, value in values.items():
        q = query_text(root, pointer)
        if not q:
            continue
        cited = citations.get(pointer)
        # Found values: query the page the vision reader cited. Missing ones: every page.
        for n in (cited.pages if value is not None and cited else every_page):
            by_page[n][pointer] = q
    page_of = {p.page: p for p in pages}
    best = {}
    for n, qs in by_page.items():
        aliases = {f"q{i}": pointer for i, pointer in enumerate(qs)}
        answers, _ = aws.textract_queries(page_of[n].png, {a: qs[p] for a, p in aliases.items()})
        for alias, ans in answers.items():
            pointer = aliases[alias]
            if ans.get("value") and (pointer not in best or ans["confidence"] > best[pointer]["confidence"]):
                best[pointer] = {**ans, "page": n, "source_file": page_of[n].source_file}
    return best


def select(leaf, pointer, vision_value, hit, agreement) -> tuple[str | None, object]:
    """(selected_reader_id, OCR draft value when OCR is selected)."""
    if hit and agreement != "agree" and hit["confidence"] >= OCR_WINS_MIN_CONF \
            and kind(leaf, pointer) in OCR_WINS_KINDS:
        value = draft_value(leaf, pointer, hit["value"])
        if value is not None:
            return READER_ID, value
    return ("vision_primary" if vision_value is not None else None), None


def scalar_leaves(attrs: dict, prefix: str = ""):
    for k, v in attrs.items():
        p = f"{prefix}/{k}"
        if isinstance(v, dict):
            yield from scalar_leaves(v, p)
        else:
            yield p, v


def set_pointer(attrs: dict, pointer: str, value) -> None:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    node = attrs
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
