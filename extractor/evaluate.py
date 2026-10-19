"""Pure deterministic evaluation of a package from raw engine outputs (no AWS calls).

Used by the pipeline, by `recheck` (new rules on an old run) and by human decisions.
"""
from __future__ import annotations

import copy

from .config import SCHEMAS
from .cross_checks import (
    completeness, completeness_checks, document_checks, draft_client_message, draft_client_messages, model_checks, package_checks,
    page_gaps, recommended_next_step,
)
from .normalize import normalize
from .reconcile import reconcile_fields
from .rules_engine import Rules


def _resolve_field_target(docs, target: str):
    """'payslip.last_name' (first doc of type) or 'd2.last_name' (doc_id)."""
    if "." not in target:
        raise KeyError(f"field target must be '<doc_type|doc_id>.<field>': {target}")
    who, field = target.split(".", 1)
    for d in docs:
        if (d["doc_id"] == who or d["doc_type"] == who) and field in d["fields"]:
            return d, field
    raise KeyError(f"field not found: {target}")


def apply_field_overrides(docs, overrides):
    for ov in overrides:
        d, field = _resolve_field_target(docs, ov["target"])
        f = d["fields"][field]
        f.setdefault("engine", {"value": f["value"], "status": f["status"], "outcome": f["outcome"]})
        act = ov["action"]
        if act == "confirm":
            f["status"] = "passed"
        elif act == "edit":
            ftype = SCHEMAS[d["doc_type"]]["fields"][field].get("type", "text")
            f["value"] = ov.get("new_value")
            f["normalized"] = normalize(ftype, ov.get("new_value"))
            f["status"] = "passed"
        elif act == "reject":
            f["value"], f["normalized"], f["status"] = None, None, "missing"
        f["human"] = {"action": act, "reviewer": ov.get("reviewer"), "event_hash": ov.get("event_hash")}
        if "HUMAN_DECISION" not in f["rule_ids"]:
            f["rule_ids"].append("HUMAN_DECISION")


def apply_check_overrides(checks, overrides):
    for ov in overrides:
        tgt = ov["target"]
        matches = [c for c in checks if c["check_id"] == tgt or c["rule_id"] == tgt or c["check"] == tgt]
        for c in matches:
            c.setdefault("engine", {"status": c["status"], "outcome": c["outcome"]})
            act = ov["action"]
            if act == "reject":   # reviewer says the engine's finding is wrong -> flip
                c["status"] = "passed" if c["engine"]["status"] != "passed" else "discrepancy"
            elif act == "edit":
                c["status"] = ov.get("new_value")
            # confirm: reviewer agrees, status unchanged
            c["human"] = {"action": act, "reviewer": ov.get("reviewer"), "event_hash": ov.get("event_hash")}


def _counts(items) -> dict:
    out: dict = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


def evaluate_package(raw_docs: list[dict], rules: Rules, *, field_overrides=(), check_overrides=(),
                     requested_documents=(), run_date: str | None = None) -> dict:
    docs = []
    for rd in raw_docs:
        d = {k: copy.deepcopy(v) for k, v in rd.items() if k != "raw"}
        d["fields"] = reconcile_fields(rd["doc_type"], rd["raw"]["textract"], rd["raw"]["bedrock"], rules)
        docs.append(d)
    apply_field_overrides(docs, field_overrides)

    checks = []
    for d in docs:
        dc = document_checks(d, rules, run_date=run_date)
        d["checks"] = dc
        checks.extend(dc)
    checks.extend(package_checks(docs, rules, run_date=run_date))
    required, missing, source = completeness(docs, rules)
    gaps = page_gaps(docs)
    cc, extra = completeness_checks(docs, rules, required, missing, source, gaps)
    checks.extend(cc)
    checks.extend(model_checks(docs, raw_docs, rules))
    apply_check_overrides(checks, check_overrides)
    missing = list(missing) + [r for r in requested_documents if r not in missing]

    # Cite the rules applied on each field
    by_id = {d["doc_id"]: d for d in docs}
    for c in checks:
        for ev in c.get("evidence") or []:
            f = by_id.get(ev.get("doc_id"), {}).get("fields", {}).get(ev.get("field"))
            if f is not None and c["rule_id"] not in f["rule_ids"]:
                f["rule_ids"].append(c["rule_id"])

    client = None
    for d in docs:
        fn = (d["fields"].get("first_name") or {}).get("value")
        ln = (d["fields"].get("last_name") or {}).get("value")
        if fn or ln:
            client = " ".join(x for x in (fn, ln) if x)
            break
    statuses = [f["status"] for d in docs for f in d["fields"].values()]
    n = len(statuses) or 1
    auto = sum(1 for s in statuses if s in ("passed", "warning"))
    passed = sum(1 for s in statuses if s == "passed")
    return {
        "rules_version": rules.version,
        "rules_label": rules.label,
        "documents": docs,
        "checks": checks,
        "required_documents": required,
        "required_documents_source": source,
        "missing_documents": missing,
        "missing_pages": gaps,
        "unexpected_documents": extra,
        "combined_confidence": round(auto / n, 3),
        # compat: share of fields WITHOUT manual_review/missing (passed + warning), not "passed".
        "fields_auto_validated_pct": round(100 * auto / n, 1),
        # strict KPI the review screen shows ("39/48 fields passed"): status == passed only
        "fields_passed": passed,
        "fields_total": len(statuses),
        "fields_passed_pct": round(100 * passed / n, 1),
        "field_status_counts": _counts(statuses),
        "check_status_counts": _counts(c["status"] for c in checks),
        "client_name": client,
        "recommended_next_step": recommended_next_step(rules, checks, statuses, missing, gaps),
        "draft_client_message": draft_client_message(missing, gaps, checks, client),
        "draft_client_messages": draft_client_messages(missing, gaps, checks, client),
    }
