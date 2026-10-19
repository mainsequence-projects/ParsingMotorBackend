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


def test_tax_arrears_wording():
    clear = _doc("tax_or_debt_certificate", {"arrears_status": _f("no tax arrears")})
    assert document_checks(clear, R)[0]["status"] == "passed"
    bad = _doc("tax_or_debt_certificate", {"arrears_status": _f("has outstanding tax arrears")})
    assert document_checks(bad, R)[0]["status"] == "discrepancy"
    # Czech "no arrears" must win even though the word "nedoplatky" is present
    cz = _doc("tax_or_debt_certificate", {"arrears_status": _f("nemá žádné daňové nedoplatky")})
    assert document_checks(cz, R)[0]["outcome"] == "clear"


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
