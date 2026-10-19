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


def test_page_two_of_two_continues_previous_page_even_with_misread_key():
    pages = [
        {"source_file": "scan.pdf", "page": 1, "doc_type": "bank_statement", "doc_key": "CZ44 0080 0000 6562 9203 7261",
         "page_number": None, "page_count": None, "confidence": 0.9, "reason": ""},
        {"source_file": "scan.pdf", "page": 2, "doc_type": "bank_statement", "doc_key": "CZ44 0800 0000 9535 2063 9216",
         "page_number": 2, "page_count": 2, "confidence": 0.9, "reason": ""},
    ]
    g = group_documents(pages)
    assert len(g) == 1 and [p["page"] for p in g[0]["pages"]] == [1, 2]   # unnumbered page fills slot 1


def test_page_one_with_other_key_still_starts_new_statement():
    pages = [
        {"source_file": "a.pdf", "page": 1, "doc_type": "bank_statement", "doc_key": "CZ11", "page_number": 1,
         "page_count": 1, "confidence": 0.9, "reason": ""},
        {"source_file": "a.pdf", "page": 2, "doc_type": "bank_statement", "doc_key": "CZ22", "page_number": 1,
         "page_count": 1, "confidence": 0.9, "reason": ""},
    ]
    assert len(group_documents(pages)) == 2
