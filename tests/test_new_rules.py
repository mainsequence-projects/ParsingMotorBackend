"""Cross-document rules for the Czech loan case: employer, recency, tax clearance, holder.

Why: employer_confirmation and tax_or_debt_certificate only earn their place if the engine
compares them to the rest of the package. Each test builds documents in memory (no AWS).
"""
from extractor.cross_checks import package_checks, document_checks
from extractor.rules_engine import load_rules

R = load_rules()


def _f(value, norm=None, page=1, status="passed"):
    return {"value": value, "normalized": norm if norm is not None else value, "page": page,
            "status": status, "textract": value, "bedrock": value, "outcome": "agree_exact"}


def _doc(dt, fields, doc_id="d1"):
    return {"doc_id": doc_id, "doc_type": dt, "pages": [{"page": 1}], "fields": fields}


def _status(checks, rule_id, check=None):
    for c in checks:
        if c["rule_id"] == rule_id and (check is None or c["check"] == check):
            return c["status"]
    return None


def test_employer_mismatch_is_a_discrepancy():
    docs = [
        _doc("payslip", {"employer": _f("ACME s.r.o.")}, "d1"),
        _doc("employer_confirmation", {"employer": _f("Beta Holding a.s.")}, "d2"),
    ]
    assert _status(package_checks(docs, R), "XDOC_EMPLOYER") == "discrepancy"
    # legal form and diacritics must not matter
    docs[1]["fields"]["employer"] = _f("Acme s. r. o.")
    assert _status(package_checks(docs, R), "XDOC_EMPLOYER") == "passed"


def test_certificate_older_than_30_days_is_stale():
    docs = [
        _doc("contract", {"contract_date": _f("01.10.2026", "2026-10-01")}),
        _doc("tax_or_debt_certificate", {"issue_date": _f("01.08.2026", "2026-08-01")}, "d2"),
    ]
    assert _status(package_checks(docs, R), "DOC_RECENT", "tax_certificate_recent") == "discrepancy"
    docs[1]["fields"]["issue_date"] = _f("20.09.2026", "2026-09-20")
    assert _status(package_checks(docs, R), "DOC_RECENT", "tax_certificate_recent") == "passed"
    docs[1]["fields"]["issue_date"] = _f(None, None)
    docs[1]["fields"]["issue_date"]["normalized"] = None
    assert _status(package_checks(docs, R), "DOC_RECENT", "tax_certificate_recent") == "manual_review"


def test_tax_arrears_is_a_yes_no_read_by_both_engines():
    def cert(v, status="passed"):
        f = _f(v, None, status=status)
        from extractor.normalize import norm_yesno
        f["normalized"] = norm_yesno(v)
        return _doc("tax_or_debt_certificate", {"has_tax_arrears": f})
    assert document_checks(cert("no"), R)[0]["status"] == "passed"
    assert document_checks(cert("ano"), R)[0]["status"] == "discrepancy"
    assert document_checks(cert("maybe"), R)[0]["status"] == "manual_review"
    # engines disagreed -> never 'clear' on a single reader
    assert document_checks(cert("no", status="manual_review"), R)[0]["status"] == "manual_review"


def test_textract_without_czech_letters_counts_as_agreement():
    from extractor.reconcile import reconcile_fields
    out = reconcile_fields(
        "id_card",
        {"first_name": {"value": "Ondej", "confidence": 0.95, "page": 1},
         "address": {"value": "koln 2, 400 01 st nad Labem", "confidence": 0.66, "page": 1},
         "last_name": {"value": "Novak", "confidence": 0.95, "page": 1}},
        {"first_name": {"value": "Ondřej", "page": 1},
         "address": {"value": "Školní 2, 400 01 Ústí nad Labem", "page": 1},
         "last_name": {"value": "Nováková", "page": 1}}, R)
    assert out["first_name"]["outcome"] == "agree_ocr_no_diacritics"
    assert out["first_name"]["value"] == "Ondřej" and out["first_name"]["status"] == "passed"
    assert out["address"]["value"] == "Školní 2, 400 01 Ústí nad Labem"
    assert out["last_name"]["outcome"] == "disagree"          # real difference, not just accents


def test_czech_month_without_diacritics_from_ocr():
    from extractor.normalize import norm_date
    assert norm_date("2.rjna 2026") == "2026-10-02"
    assert norm_date("15. zari 2026") == "2026-09-15"


def test_certificate_holder_must_match_the_id():
    idc = _doc("id_card", {"first_name": _f("Jana", "jana"), "last_name": _f("Nováková", "novakova"),
                           "birth_number": _f("885412/0000", "8854120000")})
    cert = _doc("tax_or_debt_certificate",
                {"holder_name": _f("Petr Svoboda", "petrsvoboda"),
                 "birth_number": _f("700101/0001", "7001010001")}, "d2")
    assert _status(package_checks([idc, cert], R), "XDOC_HOLDER_RC") == "discrepancy"
    cert["fields"]["holder_name"] = _f("Jana Nováková", "jananovakova")
    cert["fields"]["birth_number"] = _f("885412/0000", "8854120000")
    assert _status(package_checks([idc, cert], R), "XDOC_HOLDER_RC") == "passed"


def test_income_vs_employer_confirmation():
    docs = [
        _doc("contract", {"declared_monthly_income": _f(52000, 52000.0)}),
        _doc("employer_confirmation", {"monthly_net_income": _f(38000, 38000.0)}, "d2"),
    ]
    assert _status(package_checks(docs, R), "XDOC_INCOME", "income_contract_vs_employer") == "discrepancy"


def test_bilingual_draft_covers_missing_and_discrepancy():
    from extractor.cross_checks import draft_client_messages
    msgs = draft_client_messages(
        ["tax_or_debt_certificate"], [{"doc_type": "bank_statement", "page_count": 2,
                                       "missing_page_numbers": [2]}],
        [{"rule_id": "XDOC_EMPLOYER", "status": "discrepancy"}], "Jana Nováková")
    assert "potvrzení o bezdlužnosti" in msgs["cs"] and "strana" in msgs["cs"].lower()
    assert "employer" in msgs["en"].lower() and "page(s) 2 of 2" in msgs["en"]
    assert msgs["cs"] != msgs["en"]


def test_ocr_amounts_with_lost_or_spaced_decimal_comma():
    from extractor.normalize import norm_amount
    assert norm_amount("480 000 00 K") == 480000.0       # 'Kč' -> 'K', comma lost
    assert norm_amount("27731 29 Kc") == 27731.29
    assert norm_amount("47 200, 00 Kc") == 47200.0
    assert norm_amount("1O 000") is None                   # letter inside: still refused


def test_employment_term_in_czech_or_english_is_the_same_value():
    from extractor.normalize import norm_employment_term as t
    assert t("Indefinite") == t("na dobu neurčitou") == "indefinite"
    assert t("na dobu určitou do 31. 12. 2027") == "fixed"
    assert t("") is None


def test_company_legal_form_variants():
    from extractor.cross_checks import _company
    assert _company("Vltava Software s.r.o") == _company("Vltava Software, s. r. o.") == "vltavasoftware"
    assert _company("Moravská strojírna a.s.") == "moravskastrojirna"


def test_recency_never_decided_on_a_date_the_engines_disagree_on():
    f = _f("01.02.2016", None, status="manual_review")
    f["normalized"] = "2016-02-01"
    doc = _doc("employer_confirmation", {"issue_date": f})
    c = [x for x in document_checks(doc, R, run_date="2026-10-02") if x["rule_id"] == "DOC_RECENT"]
    if not c:   # DOC_RECENT is a package check in some versions
        from extractor.cross_checks import _recency
        c = _recency([doc], R, "2026-10-02")
    assert c[0]["status"] == "manual_review" and "not confirmed" in c[0]["detail"]
