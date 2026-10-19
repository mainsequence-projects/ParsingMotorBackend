"""Rule-learning loop: human decisions -> concrete rules.yaml proposal -> human review -> new version."""
import copy

import pytest
import yaml

from extractor import audit, rule_changes
from extractor.rules_engine import DEFAULT_RULES_PATH, RulesConfigError, load_rules, load_rules_text
from tests.test_audit import _fake_run


@pytest.fixture
def decided(audit_dir, raw03):
    """3 runs where the reviewer confirms a low-confidence IBAN and rejects the income discrepancy."""
    raw = copy.deepcopy(raw03)
    raw[2]["raw"]["textract"]["iban"]["confidence"] = 0.45      # agree_low_conf -> warning
    for _ in range(3):
        rid = _fake_run(raw)
        audit.record_decision(rid, {"field": "bank_statement.iban", "action": "confirm", "reviewer": "ana"})
        audit.record_decision(rid, {"check": "XDOC_INCOME:income_contract_vs_payslip:package",
                                    "action": "reject", "reviewer": "ana"})
    return audit_dir


def _by_path(props):
    return {(p["rule_id"], p["path"]): p for p in props["proposals"]}


def test_patch_changes_one_value_and_keeps_comments():
    text = DEFAULT_RULES_PATH.read_text(encoding="utf-8")
    new = rule_changes.patch_rules_text(text, "FIELD_RECONCILE", "params.pass_conf", 0.8)
    assert new.count("#") == text.count("#")
    old_d, new_d = yaml.safe_load(text), yaml.safe_load(new)
    fr = next(r for r in new_d["rules"] if r["id"] == "FIELD_RECONCILE")
    assert fr["params"]["pass_conf"] == 0.8
    fr["params"]["pass_conf"] = 0.9
    assert new_d == old_d                                  # nothing else moved
    assert load_rules_text(new).version != load_rules_text(text).version


def test_patch_refuses_unknown_targets_and_invalid_values():
    text = DEFAULT_RULES_PATH.read_text(encoding="utf-8")
    with pytest.raises(RulesConfigError):
        rule_changes.patch_rules_text(text, "NO_SUCH_RULE", "params.x", 1)
    with pytest.raises(RulesConfigError):
        rule_changes.patch_rules_text(text, "FIELD_RECONCILE", "params.no_such_param", 1)
    with pytest.raises(RulesConfigError):                  # validator still guards the new file
        rule_changes.patch_rules_text(text, "FIELD_RECONCILE", "params.pass_conf", 7)


def test_decisions_become_concrete_proposals_with_impact(decided):
    before = DEFAULT_RULES_PATH.read_bytes()
    props = rule_changes.proposals(min_support=3)
    got = _by_path(props)
    pc = got[("FIELD_RECONCILE", "params.pass_conf")]
    assert (pc["old"], pc["new"]) == (0.9, 0.45)
    inc = got[("XDOC_INCOME", "severity.out_of_tolerance")]
    assert (inc["old"], inc["new"]) == ("discrepancy", "manual_review")   # one step milder, never 'passed'
    assert inc["support"] == 3 and inc["impact"]["runs_evaluated"] == 3
    assert inc["candidate_rules_version"] != props["rules_version"]
    assert props["auto_applied"] is False
    assert DEFAULT_RULES_PATH.read_bytes() == before        # proposing never touches rules.yaml


def test_approve_writes_new_version_and_ledger(decided, tmp_path):
    before = DEFAULT_RULES_PATH.read_bytes()
    inc = _by_path(rule_changes.proposals())[("XDOC_INCOME", "severity.out_of_tolerance")]
    out = tmp_path / "rules.yaml"
    r = rule_changes.review(inc["proposal_id"], "approve", "lead_ana", comment="bonus months",
                            write_to=out)
    new = load_rules(out)
    assert new.version == r["rules_version_new"] == inc["candidate_rules_version"]
    assert new.severity("XDOC_INCOME", "out_of_tolerance") == "manual_review"
    assert (decided / "rules" / f"rules-{new.version}.yaml").exists()
    assert audit.verify(path=decided / "rules_ledger.jsonl")["ok"]
    assert r["event"]["payload"]["reviewer"] == "lead_ana"
    assert DEFAULT_RULES_PATH.read_bytes() == before        # only the explicit target is written
    # against the new version the same change is no longer proposed
    assert ("XDOC_INCOME", "severity.out_of_tolerance") not in _by_path(rule_changes.proposals(rules=new))


def test_rejected_proposal_is_not_offered_again(decided):
    pc = _by_path(rule_changes.proposals())[("FIELD_RECONCILE", "params.pass_conf")]
    rule_changes.review(pc["proposal_id"], "reject", "lead_ana", comment="sample too small")
    assert ("FIELD_RECONCILE", "params.pass_conf") not in _by_path(rule_changes.proposals())
    assert rule_changes.proposals()["history"][-1]["type"] == "rule_change_rejected"


def test_review_validates_input(decided):
    with pytest.raises(audit.Conflict):
        rule_changes.review("deadbeef0000", "approve", "ana")
    pid = rule_changes.proposals()["proposals"][0]["proposal_id"]
    with pytest.raises(audit.Invalid):
        rule_changes.review(pid, "approve", "  ")
    with pytest.raises(audit.Invalid):
        rule_changes.review(pid, "merge", "ana")


def test_api_proposals_and_review(decided):
    from fastapi.testclient import TestClient
    from extractor.api import app
    client = TestClient(app)
    r = client.get("/rules/proposals?min_support=3")
    assert r.status_code == 200 and r.json()["proposals"]
    assert client.post("/rules/proposals/deadbeef0000/review",
                       json={"action": "approve", "reviewer": "ana"}).status_code == 409
    pid = r.json()["proposals"][0]["proposal_id"]
    ok = client.post(f"/rules/proposals/{pid}/review", json={"action": "approve", "reviewer": "ana"})
    assert ok.status_code == 200 and ok.json()["rules_version_new"]
    assert "EXTRACTOR_RULES" in ok.json()["activation"]
    assert client.get("/rules").json()["rules_version"] != ok.json()["rules_version_new"]  # not hot-swapped
