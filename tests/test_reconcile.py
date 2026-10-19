"""Reconciler citations (no AWS)."""
from extractor.reconcile import reconcile_fields
from extractor.rules_engine import load_rules

R = load_rules()


def test_page_cites_the_engine_whose_value_was_kept():
    t = {"loan_amount": {"value": "250000.00", "confidence": 0.5, "page": 1, "bbox": {"Left": 0.1}}}
    b = {"loan_amount": {"value": 520000.0, "page": 3}}
    f = reconcile_fields("contract", t, b, R)["loan_amount"]
    assert f["outcome"] == "disagree" and f["source"] == "bedrock"   # amount: low OCR conf -> Bedrock
    assert f["value"] == 520000.0 and f["page"] == 3
    assert f["bbox"] is None and f["confidence"] is None
    assert f["textract_page"] == 1 and f["bedrock_page"] == 3          # both readings still shown


def test_llm_only_cites_llm_page_and_agreement_cites_ocr():
    f = reconcile_fields("contract", {}, {"loan_amount": {"value": 1.0, "page": 2}}, R)["loan_amount"]
    assert f["source"] == "bedrock" and f["page"] == 2
    t = {"loan_amount": {"value": "1.00", "confidence": 0.95, "page": 1}}
    f = reconcile_fields("contract", t, {"loan_amount": {"value": 1.0, "page": 1}}, R)["loan_amount"]
    assert f["source"] == "both" and f["page"] == 1 and f["confidence"] == 0.95
