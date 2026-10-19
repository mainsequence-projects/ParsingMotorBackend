"""Next-step policy edge cases (no AWS)."""
import copy

from extractor.evaluate import evaluate_package
from extractor.rules_engine import load_rules


def _all_agree(raw):
    """Make every field agree at high confidence so only the case under test matters."""
    raw = copy.deepcopy(raw)
    for d in raw:
        for k, b in d["raw"]["bedrock"].items():
            if k == "required_documents":
                continue
            d["raw"]["textract"][k] = {"value": b["value"], "confidence": 0.99, "page": b.get("page") or 1}
    return raw


def test_missing_key_field_never_approves(raw04):
    raw = _all_agree(raw04)
    for d in raw:   # fix 04's injected errors so the package would otherwise be clean
        if d["doc_type"] in ("id_card", "payslip"):
            for eng in ("textract", "bedrock"):
                if "birth_number" in d["raw"][eng]:
                    d["raw"][eng]["birth_number"]["value"] = "885412/0000"
                if "last_name" in d["raw"][eng]:
                    d["raw"][eng]["last_name"]["value"] = "Nováková"
    idc = next(d for d in raw if d["doc_type"] == "id_card")
    idc["raw"]["textract"].pop("document_number", None)
    idc["raw"]["bedrock"]["document_number"] = {"value": None, "page": None}
    res = evaluate_package(raw, load_rules(), run_date="2026-10-19")
    idc_fields = next(d for d in res["documents"] if d["doc_type"] == "id_card")["fields"]
    assert idc_fields["document_number"]["status"] == "missing"
    assert not any(c["status"] == "discrepancy" for c in res["checks"]), [c for c in res["checks"] if c["status"] == "discrepancy"]
    assert res["recommended_next_step"] == "manual_review_extraction"
