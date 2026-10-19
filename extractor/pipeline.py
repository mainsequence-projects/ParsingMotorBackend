"""End-to-end package pipeline: ingest -> classify -> extract -> reconcile -> checks."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .aws import usage
from . import cost as costmeter
from .classify import classify_pages, group_documents
from .config import BEDROCK_MODEL, REGION
from .cross_checks import (
    completeness, cross_document_checks, draft_client_message, recommended_next_step,
)
from .extract import extract_document
from .ingest import load_bytes, load_path
from .normalize import norm_name
from .reconcile import explain_disagreement, reconcile_fields, run_field_rules


def _process_doc(doc: dict, explain: bool) -> dict:
    raw = extract_document(doc)
    fields = reconcile_fields(doc["doc_type"], raw["textract"], raw["bedrock"])
    if explain:
        for name, f in fields.items():
            if f["status"] == "manual_review":
                f["explanation"] = explain_disagreement(
                    doc["doc_type"], name, f["textract"], f["bedrock"])
    checks = run_field_rules(fields)
    return {
        "doc_type": doc["doc_type"],
        "source_file": doc["source_file"],
        "page_start": doc["pages"][0]["page"],
        "page_end": doc["pages"][-1]["page"],
        "classification_confidence": doc["confidence"],
        "classification_reason": doc.get("reason"),
        "fields": fields,
        "checks": checks,
    }


def run_package(pages, *, package_id: str = "package", explain: bool = False) -> dict:
    classified = classify_pages(pages)
    docs = group_documents(classified)
    processed = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(docs)))) as pool:
        futs = {pool.submit(_process_doc, d, explain): i for i, d in enumerate(docs)}
        ordered = [None] * len(docs)
        for fut in as_completed(futs):
            ordered[futs[fut]] = fut.result()
        processed = ordered

    all_checks = []
    for d in processed:
        all_checks.extend({**c, "doc_type": d["doc_type"]} for c in d["checks"])
    all_checks.extend(cross_document_checks(processed))

    required, missing = completeness(processed)
    # client name for the draft email
    client = None
    for d in processed:
        fn = (d["fields"].get("first_name") or {}).get("value")
        ln = (d["fields"].get("last_name") or {}).get("value")
        if fn or ln:
            client = " ".join(x for x in (fn, ln) if x)
            break

    field_statuses = [f["status"] for d in processed for f in d["fields"].values()]
    n_fields = len(field_statuses) or 1
    n_auto = sum(1 for s in field_statuses if s in ("passed", "warning"))
    combined = round(n_auto / n_fields, 3)

    return {
        "package_id": package_id,
        "engine_version": __version__,
        "region": REGION,
        "bedrock_model": BEDROCK_MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "documents": [
            {k: v for k, v in d.items() if k != "checks"} | {"checks": d["checks"]}
            for d in processed
        ],
        "required_documents": required,
        "missing_documents": missing,
        "checks": all_checks,
        "combined_confidence": combined,
        "fields_auto_validated_pct": round(100 * combined, 1),
        "recommended_next_step": recommended_next_step(missing, all_checks, field_statuses),
        "draft_client_message": draft_client_message(missing, all_checks, client),
        "usage": dict(usage),
        "cost": costmeter.snapshot(),
        "pages_classified": [
            {"source_file": c["source_file"], "page": c["page"],
             "doc_type": c["doc_type"], "confidence": c["confidence"],
             "reason": c["reason"]}
            for c in classified
        ],
    }


def run_path(path: str | Path, **kwargs) -> dict:
    path = Path(path)
    pages = load_path(path)
    return run_package(pages, package_id=path.name, **kwargs)


def run_uploads(files: list[tuple[str, bytes]], **kwargs) -> dict:
    pages = []
    for name, data in files:
        pages += load_bytes(name, data)
    return run_package(pages, **kwargs)
