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


def _obs(page, kind, num, count, hint):
    return {"source_file": "package.pdf", "page": page, "document_type": kind, "confidence": 0.95,
            "printed_page_number": num, "printed_page_count": count, "group_hint": hint}


def test_v2_shuffled_pages_with_different_hints_are_merged_by_printed_numbers():
    from extractor.splitter import group_v2_pages
    pages = [  # package 18 as real Bedrock observed it
        _obs(1, "bank_statement", 2, 3, "Account 2329-2892594/0600, IBAN CZ49 0600 0023 2900 0289 2594"),
        _obs(2, "bank_statement", 3, 3, "Berounka banka, a.s. / Account 2329-2892594/0600"),
        _obs(3, "employer_confirmation", 1, 1, "IČO 25134477"),
        _obs(4, "bank_statement", 1, 3, "VÝPIS Z ÚČTU (Account Statement)"),
        _obs(5, "national_identity_card", 1, 2, "VZOR / SPECIMEN"),
        _obs(9, "payslip", None, None, "820128/7292"),
        _obs(10, "national_identity_card", 2, 2, "820128/7292"),
    ]
    got = sorted((g["doc_type"], tuple(p["page"] for p in g["pages"])) for g in group_v2_pages(pages))
    assert ("bank_statement", (4, 1, 2)) in got and ("national_identity_card", (5, 10)) in got
    assert len(got) == 4


def test_v2_two_documents_of_the_same_type_stay_apart():
    from extractor.splitter import group_v2_pages
    two_certs = [_obs(6, "tax_clearance_certificate", 1, 1, "č. j. 9882544/26/2273"),
                 _obs(9, "tax_clearance_certificate", 1, 1, "č. j. 1111111/26/2273")]
    assert len(group_v2_pages(two_certs)) == 2
    other_account = [_obs(1, "bank_statement", 1, 2, "Account 2329-2892594/0600"),
                     _obs(2, "payslip", None, None, None),
                     _obs(5, "bank_statement", 2, 2, "Account 9999-1234567/0100")]
    assert len(group_v2_pages(other_account)) == 3
