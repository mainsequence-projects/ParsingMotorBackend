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
