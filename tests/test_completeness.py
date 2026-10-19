"""Completeness: the Contract defines the package (no AWS)."""
from extractor.evaluate import evaluate_package
from extractor.rules_engine import load_rules


def test_missing_contract_is_a_missing_document(raw03):
    no_contract = [d for d in raw03 if d["doc_type"] != "contract"]
    res = evaluate_package(no_contract, load_rules(), run_date="2026-10-19")
    assert "contract" in res["missing_documents"]
    assert res["required_documents_source"] == "default"
    assert res["recommended_next_step"] in ("request_missing_documents", "manual_review_discrepancy")
    assert res["recommended_next_step"] != "approve_ready"
    assert "loan contract" in res["draft_client_message"]


def test_contract_present_is_listed_but_not_missing(raw03):
    res = evaluate_package(raw03, load_rules(), run_date="2026-10-19")
    assert res["required_documents"][0] == "contract"
    assert "contract" not in res["missing_documents"]


def test_two_contracts_go_to_manual_review(raw03):
    import copy
    second = copy.deepcopy(next(d for d in raw03 if d["doc_type"] == "contract"))
    second["doc_id"] = "d9"
    res = evaluate_package(raw03 + [second], load_rules(), run_date="2026-10-19")
    c = next(c for c in res["checks"] if c["rule_id"] == "CONTRACT_SINGLE")
    assert c["outcome"] == "multiple" and c["status"] == "manual_review"
    assert res["recommended_next_step"] != "approve_ready"


def test_contract_single_rule_needs_both_outcomes():
    import pytest
    import yaml
    from extractor.rules_engine import DEFAULT_RULES_PATH, RulesConfigError, load_rules_text
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    next(r for r in data["rules"] if r["id"] == "CONTRACT_SINGLE")["severity"].pop("multiple")
    with pytest.raises(RulesConfigError):
        load_rules_text(yaml.safe_dump(data), "x.yaml")
