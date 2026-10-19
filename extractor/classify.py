"""Per-page classification with Bedrock (Haiku, temperature 0).

Besides the doc type, each page reports layout cues used by the splitter:
printed page markers ("Page 2 of 3", "Strana 1 z 2"), a doc_key that links pages of the same
document (IBAN, contract no., document no.) and the rotation needed to make it upright.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .aws import converse_json
from .config import BEDROCK_CLASSIFY_FALLBACKS, BEDROCK_CLASSIFY_MODEL, DOC_TYPES, MAX_WORKERS
from .ingest import Page, rotate_png, sha256

SYSTEM = (
    "You are a bank back-office document classifier. "
    "Return ONLY valid JSON. Never invent content that is not on the page."
)

PROMPT = (
    "Classify this single page of a Czech bank loan / KYC package.\n"
    f"Allowed doc_type values: {DOC_TYPES}.\n"
    "Return JSON with keys:\n"
    '  "doc_type": one of the allowed values,\n'
    '  "confidence": 0.0-1.0,\n'
    '  "reason": one short English sentence,\n'
    '  "page_number": integer ONLY if a page marker like "Page 2 of 3" / "Strana 2 z 3" / "2/3" '
    "is printed on the page, else null,\n"
    '  "page_count": the total from that same marker, else null,\n'
    '  "doc_key": an identifier printed on the page that would also appear on other pages of the '
    "same document (account IBAN, contract number, ID document number, policy number), copied "
    "exactly, else null,\n"
    '  "rotation": degrees clockwise (0, 90, 180 or 270) the image must be rotated to be upright.\n'
    "Hints: contracts mention 'smlouva'/'contract' and loan amounts; id cards show photo fields / "
    "'obcansky prukaz' / document number; bank statements show IBAN and balances; payslips show "
    "employer / net pay; utility bills are proof_of_address; anything else (insurance, letters) is other."
)


def _int_or_none(v):
    try:
        v = int(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _conf(v) -> float:
    """LLM may answer "high" or 95: never crash the run, never exceed [0, 1]."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    f = f / 100 if 1 < f <= 100 else f
    return min(1.0, max(0.0, f))


def _classify_one(page: Page) -> dict:
    data, model, meta = converse_json(PROMPT, images=[page.png], system=SYSTEM, max_tokens=300,
                                      model_id=BEDROCK_CLASSIFY_MODEL,
                                      fallbacks=BEDROCK_CLASSIFY_FALLBACKS, return_meta=True)
    if not isinstance(data, dict):
        data = {}
    doc_type = data.get("doc_type", "other")
    if doc_type not in DOC_TYPES:
        doc_type = "other"
    rot = _int_or_none(data.get("rotation")) or 0
    rot = rot if rot in (90, 180, 270) else 0
    png = page.png
    if rot:
        png = rotate_png(page.png, rot)
    pn, pc = _int_or_none(data.get("page_number")), _int_or_none(data.get("page_count"))
    if pn and pc and pn > pc:
        pn = pc = None
    return {
        "source_file": page.source_file,
        "page": page.page,
        "doc_type": doc_type,
        "confidence": _conf(data.get("confidence")),
        "reason": data.get("reason") or "",
        "page_number": pn,
        "page_count": pc,
        "doc_key": (str(data["doc_key"]).strip() or None) if data.get("doc_key") else None,
        "rotation": rot,
        "model": model,
        "cache_hit": meta.get("cache_hit"),
        "source_sha256": page.source_sha256,
        "page_sha256": page.original_sha256,
        "sha256": sha256(png),
        "png": png,
    }


def classify_pages(pages: list[Page]) -> list[dict]:
    results = [None] * len(pages)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(pages)))) as pool:
        futs = {pool.submit(_classify_one, p): i for i, p in enumerate(pages)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    return results
