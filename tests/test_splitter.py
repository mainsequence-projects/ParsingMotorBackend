from extractor.evaluate import evaluate_package
from extractor.rules_engine import load_rules
from extractor.splitter import group_documents


def P(page, doc_type, pn=None, pc=None, key=None, src="package.pdf"):
    return {"source_file": src, "page": page, "doc_type": doc_type, "confidence": 0.9, "reason": "",
            "page_number": pn, "page_count": pc, "doc_key": key}


def test_shuffled_pages_regrouped_and_ordered():
    iban = "CZ65 0800 0000 0000 1234 5678"
    pages = [P(1, "bank_statement", 2, 2, iban), P(2, "payslip"), P(3, "contract", key="KB-2026-00421"),
             P(4, "id_card", key="123456789"), P(5, "bank_statement", 1, 2, iban.replace(" ", ""))]
    g = group_documents(pages)
    assert [x["doc_type"] for x in g] == ["bank_statement", "payslip", "contract", "id_card"]
    st = g[0]
    assert [p["page"] for p in st["pages"]] == [5, 1]  # ordered by printed page number
    assert st["doc_id"] == "d1"


def test_unkeyed_page_joins_same_type_group():
    g = group_documents([P(1, "bank_statement", 1, 2, "CZ1"), P(2, "payslip"), P(3, "bank_statement", 2, 2)])
    assert len(g) == 2 and len(g[0]["pages"]) == 2


def test_repeated_page_number_starts_new_document():
    g = group_documents([P(1, "payslip", 1, 1), P(2, "payslip", 1, 1)])
    assert len(g) == 2


def test_different_keys_are_different_documents():
    g = group_documents([P(1, "id_card", key="111"), P(2, "id_card", key="222")])
    assert len(g) == 2


def test_missing_page_detected_and_step_is_request():
    g = group_documents([P(1, "id_card"), P(2, "bank_statement", 1, 2, "CZ1")])
    raws = [{"doc_id": x["doc_id"], "doc_type": x["doc_type"], "source_file": x["source_file"],
             "pages": [{k: p.get(k) for k in ("page", "page_number", "page_count")} for p in x["pages"]],
             "raw": {"textract": {}, "bedrock": {}}} for x in g]
    res = evaluate_package(raws, load_rules(), run_date="2026-10-19")
    assert res["missing_pages"] == [{"doc_id": "d2", "doc_type": "bank_statement", "page_count": 2,
                                     "missing_page_numbers": [2]}]
    pc = next(c for c in res["checks"] if c["rule_id"] == "PAGE_CONTINUITY")
    assert pc["outcome"] == "gap" and pc["status"] == "manual_review"
    assert res["recommended_next_step"] == "request_missing_documents"
    assert "page(s) 2 of 2" in res["draft_client_message"]


def test_unexpected_document_flagged():
    raws = [{"doc_id": "d1", "doc_type": t, "source_file": "x", "pages": [{"page": i}],
             "raw": {"textract": {}, "bedrock": {}}} for i, t in enumerate(["id_card", "other"], 1)]
    res = evaluate_package(raws, load_rules(), run_date="2026-10-19")
    u = next(c for c in res["checks"] if c["rule_id"] == "UNEXPECTED_DOCUMENTS")
    assert u["outcome"] == "present" and "other" in res["unexpected_documents"]
