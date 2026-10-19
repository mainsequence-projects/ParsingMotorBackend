"""Second reader for the Contract's required-document list (no AWS)."""
import copy

from extractor.doclist import parse_required_documents
from extractor.evaluate import evaluate_package
from extractor.rules_engine import load_rules

R = load_rules()
P = R.params("COMPLETENESS")["list_parser"]
LINES = [{"text": t, "confidence": 0.99, "page": 1} for t in [
    "LOAN_AMOUNT:", "250000.00 CZK", "Required documents:",
    "- Copy of national ID card (občanský průkaz)", "- Bank statement for the last 3 months",
    "- Latest payslip (výplatní lístek)", "- Proof of address (utility bill)",
    "Signed in Prague. Synthetic document for demo purposes only."]]


def test_parser_maps_bullets_to_codes():
    r = parse_required_documents(LINES, P)
    assert r["value"] == ["bank_statement", "id_card", "payslip", "proof_of_address"]
    assert r["page"] == 1 and r["confidence"] == 0.99


def test_czech_header_and_unknown_item_is_kept():
    lines = [{"text": "Požadované dokumenty", "confidence": 0.98, "page": 2},
             {"text": "1. Výpis z účtu", "confidence": 0.97, "page": 2},
             {"text": "2. Daňové přiznání za rok 2025", "confidence": 0.95, "page": 2}]
    r = parse_required_documents(lines, P)
    assert r["value"][0] == "bank_statement"
    assert r["value"][1].startswith("unknown:danove_priznani")


def test_no_header_means_no_second_reading():
    assert parse_required_documents(LINES[:2], P) is None
    assert parse_required_documents(None, P) is None


def _with_lines(raw, lines):
    raw = copy.deepcopy(raw)
    next(d for d in raw if d["doc_type"] == "contract")["raw"]["textract"]["_lines"] = lines
    return raw


def test_both_readers_agree_on_list_passes(raw03):
    res = evaluate_package(_with_lines(raw03, LINES), R, run_date="2026-10-19")
    f = next(d for d in res["documents"] if d["doc_type"] == "contract")["fields"]["required_documents"]
    assert f["outcome"] == "agree_high_conf" and f["status"] == "passed" and f["source"] == "both"


def test_item_the_llm_dropped_goes_to_manual_review(raw03):
    lines = LINES[:7] + [{"text": "- Tax return 2025", "confidence": 0.99, "page": 1}] + LINES[7:]
    res = evaluate_package(_with_lines(raw03, lines), R, run_date="2026-10-19")
    f = next(d for d in res["documents"] if d["doc_type"] == "contract")["fields"]["required_documents"]
    assert f["outcome"] == "disagree" and f["status"] == "manual_review"


def test_unknown_required_type_is_flagged_and_not_sent_to_client(raw03):
    raw = copy.deepcopy(raw03)
    c = next(d for d in raw if d["doc_type"] == "contract")
    c["raw"]["bedrock"]["required_documents"]["value"] += ["unknown:tax_return"]
    res = evaluate_package(raw, R, run_date="2026-10-19")
    chk = next(x for x in res["checks"] if x["rule_id"] == "COMPLETENESS")
    assert chk["outcome"] == "unrecognised_type" and chk["status"] == "manual_review"
    assert not any(m.startswith("unknown") for m in res["missing_documents"])
    assert "unknown" not in (res["draft_client_message"] or "")
