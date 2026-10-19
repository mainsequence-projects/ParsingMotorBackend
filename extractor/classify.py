"""Classify each page into a document type with Bedrock (temperature 0)."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .aws import converse_json
from .config import DOC_TYPES, MAX_WORKERS
from .ingest import Page

SYSTEM = (
    "You are a bank back-office document classifier. "
    "Return ONLY valid JSON. Never invent content that is not on the page."
)


def _classify_one(page: Page) -> dict:
    prompt = (
        "Classify this single page of a Czech bank loan / KYC package.\n"
        f"Allowed doc_type values: {DOC_TYPES}.\n"
        "Return JSON: {\"doc_type\": \"...\", \"confidence\": 0.0-1.0, "
        "\"reason\": \"one short sentence in English\"}.\n"
        "Hints: contracts mention 'smlouva'/'contract' and loan amounts; "
        "id cards show photo fields / 'obcansky prukaz' / document number; "
        "bank statements show IBAN and balances; payslips show employer / net pay; "
        "utility bills are proof_of_address."
    )
    data, model = converse_json(prompt, images=[page.png], system=SYSTEM, max_tokens=200)
    doc_type = data.get("doc_type", "other")
    if doc_type not in DOC_TYPES:
        doc_type = "other"
    return {
        "source_file": page.source_file,
        "page": page.page,
        "doc_type": doc_type,
        "confidence": float(data.get("confidence") or 0.0),
        "reason": data.get("reason") or "",
        "model": model,
        "png": page.png,
    }


def classify_pages(pages: list[Page]) -> list[dict]:
    results = [None] * len(pages)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(pages)))) as pool:
        futs = {pool.submit(_classify_one, p): i for i, p in enumerate(pages)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    return results


def group_documents(classified: list[dict]) -> list[dict]:
    """Split a multi-page package into contiguous same-type document groups (bonus)."""
    if not classified:
        return []
    docs, cur = [], None
    for c in classified:
        if cur is None or c["doc_type"] != cur["doc_type"] or c["source_file"] != cur["source_file"]:
            cur = {"doc_type": c["doc_type"], "source_file": c["source_file"],
                   "pages": [], "confidence": c["confidence"], "reason": c["reason"]}
            docs.append(cur)
        cur["pages"].append({"page": c["page"], "png": c["png"], "confidence": c["confidence"]})
        cur["confidence"] = min(cur["confidence"], c["confidence"])
    return docs
