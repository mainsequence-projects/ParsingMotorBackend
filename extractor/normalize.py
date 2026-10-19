"""Deterministic normalisation of extracted values (no LLM)."""
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(v):
    if v is None or str(v).strip() == "":
        return None
    s = strip_diacritics(str(v)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def norm_name(v):
    s = norm_text(v)
    return s.replace(" ", "") if s else None


def norm_date(v):
    if v is None:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})", s)
        if m:
            d, mo, y = map(int, m.groups())
        else:
            m = re.search(r"(\d{1,2})\.?\s+([A-Za-z]+)\s+(\d{4})", s)
            if not m or m.group(2).lower() not in MONTHS:
                return None
            d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def norm_birth_number(v):
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    return d or None


def norm_amount(v):
    """'250 000,00 CZK' -> 250000.0 ; '52,000.50' -> 52000.5"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = re.sub(r"[^\d,.\-]", "", str(v).replace("\u00a0", " "))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + ("." + tail if len(tail) <= 2 else tail)
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):
        s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def norm_iban(v):
    if v is None:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", str(v)).upper()
    return s or None


def norm_id(v):
    if v is None:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", strip_diacritics(str(v))).upper()
    return s or None


def norm_doc_list(v):
    if not v:
        return None
    if isinstance(v, str):
        v = re.split(r"[,;\n]", v)
    return sorted({norm_text(x).replace(" ", "_") for x in v if norm_text(x)}) or None


NORMALIZERS = {
    "name": norm_name, "text": norm_text, "address": norm_text, "date": norm_date,
    "birth_number": norm_birth_number, "amount": norm_amount, "iban": norm_iban,
    "id": norm_id, "doc_list": norm_doc_list,
}


def normalize(ftype, v):
    return NORMALIZERS.get(ftype, norm_text)(v)


def values_agree(ftype, a, b):
    """Compare two normalised values. Addresses/free text use a fixed similarity threshold."""
    if a is None or b is None:
        return False
    if ftype in ("address", "text"):
        return a == b or SequenceMatcher(None, a, b).ratio() >= 0.92
    return a == b
