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


_CURRENCY = re.compile(r"(?i)\b(czk|kc|eur|usd)\b|kč|€|\$")
_NUM = re.compile(r"-?\d[\d .,']*\d|-?\d")


def norm_amount(v):
    """Strict, deterministic amount parser. Returns None when the text is ambiguous.

    '250 000,00 CZK' -> 250000.0 ; '52,000.50' -> 52000.5 ; '52 000,- Kč' -> 52000.0
    None for OCR garbage such as '1O000' / '15200E.00' (letter inside the number) or for
    two separate numbers ('2024: 52000'): guessing would silently change the magnitude.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v)
    for ch in ("\u00a0", "\u202f", "\u2009"):
        s = s.replace(ch, " ")
    s = s.replace("\u2212", "-").replace("\u2013", "-")
    s = _CURRENCY.sub(" ", s)
    s = re.sub(r"[,.]\s*-(?!\d)", "", s)          # '52 000,-' (whole crowns)
    if re.search(r"\d[A-Za-z]|[A-Za-z]\d", s):    # OCR letter/digit confusion
        return None
    nums = _NUM.findall(s)
    if len(nums) != 1:
        return None
    n = nums[0].strip().replace("'", "")
    if " " in n:                                    # spaces only as thousands groups
        if not re.fullmatch(r"-?\d{1,3}( \d{3})+([.,]\d{1,2})?", n):
            return None
        n = n.replace(" ", "")
    if "," in n and "." in n:
        if n.rfind(",") > n.rfind("."):
            n = n.replace(".", "").replace(",", ".")
        else:
            n = n.replace(",", "")
    elif "," in n:
        head, _, tail = n.rpartition(",")
        n = head.replace(",", "") + ("." + tail if len(tail) <= 2 else tail)
    elif n.count(".") > 1 or re.search(r"\.\d{3}$", n):
        n = n.replace(".", "")
    try:
        return round(float(n), 2)
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


def similarity(a, b) -> float:
    if a is None or b is None:
        return 0.0
    return SequenceMatcher(None, str(a), str(b)).ratio()


def values_agree(ftype, a, b, text_similarity: float = 0.92):
    """Compare two normalised values. Threshold comes from rules.yaml (FIELD_RECONCILE)."""
    if a is None or b is None:
        return False
    if ftype in ("address", "text"):
        return a == b or similarity(a, b) >= text_similarity
    return a == b
