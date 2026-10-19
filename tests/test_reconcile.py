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


def _f(t_val, conf, b_val, field="iban", dt="bank_statement"):
    t = {field: {"value": t_val, "confidence": conf, "page": 1}}
    return reconcile_fields(dt, t, {field: {"value": b_val, "page": 1}}, R)[field]


def test_exact_agreement_passes_below_pass_conf():
    f = _f("CZ65 0800 0000 0000 1234 5678", 0.86, "CZ6508000000000012345678")
    assert f["outcome"] == "agree_exact" and f["status"] == "passed"


def test_exact_agreement_with_very_low_conf_stays_warning():
    f = _f("CZ65 0800 0000 0000 1234 5678", 0.45, "CZ6508000000000012345678")
    assert f["outcome"] == "agree_low_conf" and f["status"] == "warning"


def test_fuzzy_agreement_still_needs_pass_conf():
    f = _f("Vinohradská 12, 120 00 Praha2", 0.75, "Vinohradská 12, 120 00 Praha 2", "address", "id_card")
    assert f["outcome"] == "agree_low_conf" and f["status"] == "warning"


def test_disagreement_never_passes_whatever_the_conf():
    f = _f("CZ65 0800 0000 0000 1234 5679", 0.99, "CZ6508000000000012345678")
    assert f["status"] == "manual_review"


def test_rules_without_floor_behave_like_v02():
    import yaml

    from extractor.rules_engine import DEFAULT_RULES_PATH, load_rules_text
    d = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    fr = next(r for r in d["rules"] if r["id"] == "FIELD_RECONCILE")
    fr["params"].pop("agree_exact_min_conf")
    r2 = load_rules_text(yaml.safe_dump(d), "v02.yaml")
    t = {"iban": {"value": "CZ65 0800", "confidence": 0.86, "page": 1}}
    f = reconcile_fields("bank_statement", t, {"iban": {"value": "CZ650800", "page": 1}}, r2)["iban"]
    assert f["outcome"] == "agree_low_conf"
