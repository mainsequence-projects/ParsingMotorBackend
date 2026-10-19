"""End-to-end pipeline: ingest -> classify (per page) -> split -> dual extract -> evaluate -> audit."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__, audit
from . import cost as costmeter
from .aws import textract_api_info
from .classify import classify_pages
from .config import BEDROCK_CLASSIFY_MODEL, BEDROCK_MODEL, REGION
from .evaluate import evaluate_package
from .extract import extract_document
from .ingest import load_bytes, load_inputs
from .reconcile import explain_disagreement
from .rules_engine import Rules, load_rules
from .splitter import group_documents

_PAGE_KEYS = ("source_file", "page", "page_number", "page_count", "doc_key", "rotation",
              "confidence", "page_sha256", "sha256", "source_sha256", "model")


def _raw_doc(group: dict) -> dict:
    raw = extract_document(group)
    return {
        "doc_id": group["doc_id"],
        "doc_type": group["doc_type"],
        "source_file": group["source_file"],
        "doc_key": group.get("doc_key"),
        "page_start": group["pages"][0]["page"],
        "page_end": group["pages"][-1]["page"],
        "pages": [{k: p.get(k) for k in _PAGE_KEYS} for p in group["pages"]],
        "classification_confidence": group["confidence"],
        "classification_reason": group.get("reason"),
        "raw": {"textract": raw["textract"], "bedrock": raw["bedrock"]},
        "calls": raw["calls"],
    }


def _field_audit(docs):
    rows = []
    for d in docs:
        for name, f in d["fields"].items():
            rows.append({"doc_id": d["doc_id"], "doc_type": d["doc_type"], "field": name,
                         "textract": {"value": f["textract"], "conf": f["textract_conf"],
                                      "page": f["textract_page"]},
                         "bedrock": {"value": f["bedrock"], "page": f["bedrock_page"]},
                         "final": f["value"], "normalized": f["normalized"],
                         "outcome": f["outcome"], "status": f["status"], "rule_ids": f["rule_ids"]})
    return rows


def run_package(pages, *, inputs=None, package_id: str = "package", explain: bool = False,
                rules: Rules | None = None, audit_enabled: bool = True) -> dict:
    rules = rules or load_rules()
    before = costmeter.snapshot()
    run_id = audit.new_run_id()
    models = {"classification": BEDROCK_CLASSIFY_MODEL, "extraction": BEDROCK_MODEL,
              "textract": textract_api_info()}
    gi = audit.git_info()
    if audit_enabled:
        audit.append(run_id, "run_started", {
            "package_id": package_id, "engine_version": __version__, "git": gi, "region": REGION,
            "rules": {"version": rules.version, "sha256": rules.sha256, "label": rules.label,
                      "path": rules.path, "text": rules.raw_text},
            "models": models,
            "inputs": inputs or [],
            "pages": [{"source_file": p.source_file, "page": p.page, "sha256": p.sha256,
                       "source_sha256": p.source_sha256} for p in pages],
        })
        audit.index_append({"run_id": run_id, "event": "run_started", "package_id": package_id,
                            "rules_version": rules.version, "git_commit": gi["commit"]})
    try:
        classified = classify_pages(pages)
        if audit_enabled:
            audit.append(run_id, "classification", {"pages": [
                {k: c.get(k) for k in _PAGE_KEYS + ("doc_type", "reason", "model", "cache_hit")}
                for c in classified]})
        groups = group_documents(classified)
        raw_docs = [None] * len(groups)
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(groups)))) as pool:
            futs = {pool.submit(_raw_doc, g): i for i, g in enumerate(groups)}
            for fut in as_completed(futs):
                raw_docs[futs[fut]] = fut.result()
        if audit_enabled:
            for rd in raw_docs:
                audit.append(run_id, "extraction", {"document": {k: v for k, v in rd.items() if k != "calls"},
                                                    "calls": rd["calls"]})
        # same reference date as /runs/{id} re-evaluations (not the box's local "today")
        result = evaluate_package(raw_docs, rules, run_date=audit.run_date(run_id))
    except Exception as e:
        if audit_enabled:
            audit.append(run_id, "run_failed", {"error": f"{type(e).__name__}: {e}",
                                                "cost": costmeter.run_delta(before)})
        raise

    if explain:
        for d in result["documents"]:
            for name, f in d["fields"].items():
                if f["status"] == "manual_review":
                    f["explanation"] = explain_disagreement(d["doc_type"], name, f["textract"], f["bedrock"])

    run_cost = costmeter.run_delta(before)
    cache_hits = {"textract": sum(1 for rd in raw_docs for c in rd["calls"]["textract"] if c.get("cache_hit")),
                  "textract_calls": sum(len(rd["calls"]["textract"]) for rd in raw_docs),
                  "bedrock_extraction": sum(1 for rd in raw_docs if (rd["calls"]["bedrock"] or {}).get("cache_hit")),
                  "classification": sum(1 for c in classified if c.get("cache_hit"))}
    if audit_enabled:
        audit.append(run_id, "evaluation", {
            "rules_version": rules.version, "fields": _field_audit(result["documents"]),
            "checks": result["checks"], "missing_documents": result["missing_documents"],
            "missing_pages": result["missing_pages"],
            "unexpected_documents": result["unexpected_documents"],
            "next_step": result["recommended_next_step"]})
        audit.append(run_id, "run_completed", {"cost": run_cost, "cache_hits": cache_hits,
                                               "cumulative_usd": costmeter.snapshot()["total_usd"]})
        audit.index_append({"run_id": run_id, "event": "run_completed", "package_id": package_id,
                            "next_step": result["recommended_next_step"], "usd": run_cost["usd"]})

    for d, rd in zip(result["documents"], raw_docs):
        d.pop("calls", None)
    return {
        "run_id": run_id,
        "package_id": package_id,
        "engine_version": __version__,
        "git_commit": gi["commit"],
        "region": REGION,
        "models": models,
        "bedrock_model": BEDROCK_MODEL,
        "generated_at": audit.utcnow(),
        **result,
        "inputs": inputs or [],
        "pages_classified": [
            {k: c.get(k) for k in ("source_file", "page", "doc_type", "confidence", "reason",
                                   "page_number", "page_count", "doc_key", "rotation", "cache_hit")}
            for c in classified],
        "cost": {"run": run_cost, "cache_hits": cache_hits, "ledger": _public_ledger(costmeter.snapshot())},
        "audit_file": audit.run_path(run_id).name if audit_enabled else None,
    }


def run_path(paths, **kwargs) -> dict:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    pages, inputs = load_inputs(paths)
    pid = kwargs.pop("package_id", None) or Path(paths[0]).name
    return run_package(pages, inputs=inputs, package_id=pid, **kwargs)


def _public_ledger(snap: dict) -> dict:
    """Ledger totals for the client; the ledger file's server path stays on the server."""
    out = dict(snap)
    if out.get("ledger"):
        out["ledger"] = Path(str(out["ledger"])).name
    return out


def run_uploads(files: list[tuple[str, bytes]], **kwargs) -> dict:
    from .ingest import check_package, safe_name, sha256
    pages, inputs, seen = [], [], set()
    for original, data in files:
        name = safe_name(original, seen)
        inputs.append({"name": name, "original_name": original, "bytes": len(data), "sha256": sha256(data)})
        pages += load_bytes(name, data)
        check_package(pages, len(inputs))     # stop before rendering more than the limit
    check_package(pages, len(inputs))
    return run_package(pages, inputs=inputs, **kwargs)
