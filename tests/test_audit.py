import json
import shutil

import pytest

from extractor import audit
from extractor.rules_engine import DEFAULT_RULES_PATH, load_rules


def _fake_run(raws, package_id="t03"):
    """Write an audit log exactly as the pipeline would, from stored raw outputs (no AWS)."""
    r = load_rules()
    rid = audit.new_run_id()
    audit.append(rid, "run_started", {"package_id": package_id, "engine_version": "test",
                                      "rules": {"version": r.version, "sha256": r.sha256, "label": r.label,
                                                "path": r.path, "text": r.raw_text},
                                      "inputs": [{"name": "x.pdf", "sha256": "ab" * 32}]})
    for d in raws:
        audit.append(rid, "extraction", {"document": d})
    return rid


def test_untouched_log_verifies(audit_dir, raw03):
    rid = _fake_run(raw03)
    v = audit.verify(rid)
    assert v["ok"] and v["events"] == 1 + len(raw03)


def test_tampered_copy_fails(audit_dir, raw03, tmp_path):
    rid = _fake_run(raw03)
    src = audit.run_path(rid)
    bad = tmp_path / "tampered.jsonl"
    shutil.copy(src, bad)
    lines = bad.read_text().splitlines()
    ev = json.loads(lines[4])  # payslip extraction event
    ev["payload"]["document"]["raw"]["textract"]["net_income"]["value"] = "52000.00"
    lines[4] = json.dumps(ev)
    bad.write_text("\n".join(lines) + "\n")
    v = audit.verify(path=bad)
    assert not v["ok"] and any("hash mismatch" in e for e in v["errors"])
    assert audit.verify(rid)["ok"]  # original untouched


def test_deleted_event_fails(audit_dir, raw03, tmp_path):
    rid = _fake_run(raw03)
    bad = tmp_path / "deleted.jsonl"
    lines = audit.run_path(rid).read_text().splitlines()
    bad.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    v = audit.verify(path=bad)
    assert not v["ok"] and any("prev_hash" in e or "seq" in e for e in v["errors"])


def test_decisions_append_and_recompute(audit_dir, raw03):
    rid = _fake_run(raw03)
    n0 = len(audit.read_events(rid))
    s0 = audit.current_state(rid)
    assert s0["recommended_next_step"] == "manual_review_discrepancy"
    # human corrects the payslip net income -> contract-vs-payslip passes, payslip-vs-statement now fails
    r = audit.record_decision(rid, {"field": "payslip.net_income", "action": "edit", "new_value": "52000",
                                    "reviewer": "ana", "comment": "re-read from original"})
    st = audit.current_state(rid)
    chk = {c["check_id"]: c["status"] for c in st["checks"]}
    assert chk["XDOC_INCOME:income_contract_vs_payslip:package"] == "passed"
    assert chk["XDOC_INCOME:income_payslip_vs_statement:package"] == "discrepancy"
    f = next(d for d in st["documents"] if d["doc_type"] == "payslip")["fields"]["net_income"]
    assert f["normalized"] == 52000.0 and f["human"]["reviewer"] == "ana" and "HUMAN_DECISION" in f["rule_ids"]
    assert f["engine"]["value"] != "52000"  # engine reading preserved
    ev = audit.read_events(rid)
    assert len(ev) == n0 + 2 and ev[-2]["type"] == "decision" and ev[-1]["type"] == "reevaluation"
    assert ev[-2]["payload"]["before"]["value"] == f["engine"]["value"]
    assert audit.verify(rid)["ok"]


def test_reject_findings_then_request_documents(audit_dir, raw03):
    rid = _fake_run(raw03)
    for cid in ("XDOC_INCOME:income_contract_vs_payslip:package",
                "XDOC_INCOME:income_contract_vs_statement:package"):
        audit.record_decision(rid, {"check": cid, "action": "reject", "reviewer": "ana",
                                    "comment": "bonus month, explained by client"})
    assert audit.current_state(rid)["recommended_next_step"] == "spot_check_warnings"
    r = audit.record_decision(rid, {"action": "request_documents", "new_value": ["payslip"],
                                    "reviewer": "ana", "comment": "need previous month payslip"})
    assert r["next_step"] == "request_missing_documents"
    assert "payslip" in audit.current_state(rid)["missing_documents"]


def test_bad_decisions_rejected(audit_dir, raw03):
    rid = _fake_run(raw03)
    with pytest.raises(audit.AuditError):
        audit.record_decision(rid, {"field": "payslip.net_income", "action": "approve", "reviewer": "x"})
    with pytest.raises(audit.AuditError):
        audit.record_decision(rid, {"field": "payslip.nope", "action": "confirm", "reviewer": "x"})
    with pytest.raises(audit.AuditError):
        audit.record_decision(rid, {"field": "payslip.net_income", "action": "confirm"})


def test_decision_refused_on_broken_chain(audit_dir, raw03):
    rid = _fake_run(raw03)
    p = audit.run_path(rid)
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"doc_type":"contract"', '"doc_type":"other"')
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(audit.AuditError, match="broken"):
        audit.record_decision(rid, {"field": "payslip.net_income", "action": "confirm", "reviewer": "x"})


def test_learning_report_suggests_never_applies(audit_dir, raw03):
    before = DEFAULT_RULES_PATH.read_bytes()
    for _ in range(3):
        rid = _fake_run(raw03)
        audit.record_decision(rid, {"field": "bank_statement.iban", "action": "confirm", "reviewer": "ana"})
        audit.record_decision(rid, {"check": "XDOC_INCOME:income_contract_vs_payslip:package",
                                    "action": "reject", "reviewer": "ana"})
    rep = audit.learning_report(min_support=3)
    assert rep["auto_applied"] is False and rep["runs_scanned"] == 3
    assert rep["per_rule"]["XDOC_INCOME"]["reject"] == 3
    rules_suggested = {s["rule_id"] for s in rep["suggestions"]}
    assert {"XDOC_INCOME", "FIELD_RECONCILE"} <= rules_suggested
    assert DEFAULT_RULES_PATH.read_bytes() == before
