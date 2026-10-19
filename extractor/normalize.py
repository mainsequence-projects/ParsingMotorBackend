"""Deterministic normalisation of extracted values (no LLM)."""
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
MONTHS |= {m[:3]: i for m, i in list(MONTHS.items())}
# Czech, without diacritics: nominative + genitive ('1. května 1988' -> kvetna)
for _i, _forms in enumerate([("leden", "ledna"), ("unor", "unora"), ("brezen", "brezna"),
                             ("duben", "dubna"), ("kveten", "kvetna"), ("cerven", "cervna"),
                             ("cervenec", "cervence"), ("srpen", "srpna"), ("zari",),
                             ("rijen", "rijna"), ("listopad", "listopadu"), ("prosinec", "prosince")]):
    for _f in _forms:
        MONTHS[_f] = _i + 1


def _ocr_variants(word: str):
    """Spellings an OCR without Czech support produces: each accented letter dropped or plain."""
    import itertools
    import unicodedata as _u
    opts = []
    for ch in word:
        base = "".join(c for c in _u.normalize("NFKD", ch) if not _u.combining(c))
        opts.append({base, ""} if base != ch else {ch})
    return {"".join(t) for t in itertools.product(*opts)}


# Textract does not support Czech: 'října' comes back as 'rjna' / 'rijna'. Add those variants
# unless they collide with another month (then the date stays unparseable -> human review).
_cz_full = [("ledna",), ("února", "únor"), ("března", "březen"), ("dubna",), ("května", "květen"),
            ("června", "červen"), ("července", "červenec"), ("srpna",), ("září",), ("října", "říjen"),
            ("listopadu",), ("prosince",)]
_seen: dict = {}
for _i, _forms in enumerate(_cz_full):
    for _f in _forms:
        for _v in _ocr_variants(_f):
            if len(_v) >= 3:
                _seen.setdefault(_v, set()).add(_i + 1)
for _v, _ms in _seen.items():
    if len(_ms) == 1 and _v not in MONTHS:
        MONTHS[_v] = next(iter(_ms))


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
            t = strip_diacritics(s).lower()
            m = re.search(r"(\d{1,2})\.?\s*([a-z]+)\.?,?\s+(\d{4})", t)
            if m and m.group(2) in MONTHS:
                d, mo, y = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
            else:
                m = re.search(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", t)   # 'April 12, 1988'
                if not m or m.group(1) not in MONTHS:
                    return None
                d, mo, y = int(m.group(2)), MONTHS[m.group(1)], int(m.group(3))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def norm_birth_number(v):
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    return d or None


_CURRENCY = re.compile(r"(?i)\b(czk|kc|eur|usd)\b|kč|€|\$|(?<=\d )K\s*$")   # "K": OCR dropped the č of Kč
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
    s = re.sub(r"(\d)([.,])\s+(\d{2})(?!\d)", r"\1\2\3", s)  # OCR '47 200, 00'
    # OCR lost the decimal comma: '480 000 00' / '27731 29' -> a final 2-digit group can only be
    # haléře (thousands groups always have 3 digits). Still has to agree with the LLM to pass.
    s = re.sub(r"^(\s*-?(?:\d{1,3}(?: \d{3})+|\d{4,})) (\d{2})\s*$", r"\1,\2", s)
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


def norm_yesno(v):
    """yes/no answers (Textract QUERIES and the LLM answer the same question) -> bool."""
    if isinstance(v, bool):
        return v
    t = norm_text(v)
    if not t:
        return None
    first = t.split()[0]
    if first in ("no", "ne", "false", "n", "none"):
        return False
    if first in ("yes", "ano", "true", "y"):
        return True
    return None


def ocr_dropped_diacritics(ocr, llm) -> bool:
    """True if `ocr` is `llm` with Czech accented letters dropped or flattened.

    Textract has no Czech model: 'Ondřej' -> 'Ondej', 'Ústí nad Labem' -> 'st nad Labem'.
    Deterministic, letter by letter: only accented letters may differ, everything else must match.
    """
    if not isinstance(ocr, str) or not isinstance(llm, str) or not ocr.strip() or not llm.strip():
        return False
    # OCR keeps some accents (á, é) and drops others (ř, č, š) and sometimes the space before
    # a dropped capital ('Lucie Černá' -> 'Lucieerná'): compare without spaces and accents.
    o = re.sub(r"[\s.,;:'\"()-]+", "", strip_diacritics(ocr.strip().lower()))
    pat = []
    for ch in re.sub(r"[\s.,;:'\"()-]+", "", llm.strip().lower()):
        if ord(ch) < 128:
            pat.append(re.escape(ch))
        else:
            base = strip_diacritics(ch)
            pat.append(f"(?:{re.escape(base)})?" if base and ord(base[0]) < 128 else "")
    if not any(ord(c) > 127 for c in llm):
        return False                     # nothing accented: a plain mismatch, not this case
    return re.fullmatch("".join(pat), o) is not None


def norm_employment_term(v):
    """Employment contract term -> 'indefinite' | 'fixed' (Czech or English wording)."""
    t = strip_diacritics(str(v or "")).lower()
    if not t.strip():
        return None
    if re.search(r"neurcit|indefinite|permanent|unlimited", t):
        return "indefinite"
    if re.search(r"urcit|fixed|temporary|limited", t):
        return "fixed"
    return None


NORMALIZERS = {
    "name": norm_name, "text": norm_text, "address": norm_text, "date": norm_date,
    "birth_number": norm_birth_number, "amount": norm_amount, "iban": norm_iban,
    "id": norm_id, "doc_list": norm_doc_list, "yesno": norm_yesno,
    "employment_term": norm_employment_term,
}


def normalize(ftype, v):
    return NORMALIZERS.get(ftype, norm_text)(v)


def similarity(a, b) -> float:
    if a is None or b is None:
        return 0.0
    return SequenceMatcher(None, str(a), str(b)).ratio()
