"""Cross-document consistency and completeness vs the contract's required list."""
from .config import AMOUNT_TOLERANCE
from .normalize import norm_name, normalize


def _first(docs, doc_type, field):
    for d in docs:
        if d["doc_type"] != doc_type:
            continue
        f = d["fields"].get(field)
        if f and f.get("normalized") is not None:
            return f
    return None


def _any_name(docs):
    """Collect normalised person names across the package."""
    names = []
    for d in docs:
        for key in ("first_name", "last_name", "account_holder", "full_name"):
            f = d["fields"].get(key)
            if f and f.get("normalized"):
                names.append((d["doc_type"], key, f["normalized"], f.get("page"), f.get("value")))
    return names


def cross_document_checks(docs: list[dict]) -> list[dict]:
    checks = []
    # Same birth number across docs that carry it
    bns = []
    for d in docs:
        f = d["fields"].get("birth_number")
        if f and f.get("normalized"):
            bns.append((d["doc_type"], f["normalized"], f.get("page")))
    if len(bns) >= 2:
        vals = {b[1] for b in bns}
        checks.append({
            "check": "birth_number_consistent",
            "status": "passed" if len(vals) == 1 else "discrepancy",
            "detail": f"values={sorted(vals)}",
            "page": bns[0][2],
        })

    # First+last name consistency (id vs contract vs payslip)
    firsts = [(d["doc_type"], d["fields"]["first_name"]) for d in docs
              if d["fields"].get("first_name", {}).get("normalized")]
    lasts = [(d["doc_type"], d["fields"]["last_name"]) for d in docs
             if d["fields"].get("last_name", {}).get("normalized")]
    if len(firsts) >= 2:
        vals = {f[1]["normalized"] for f in firsts}
        checks.append({
            "check": "first_name_consistent",
            "status": "passed" if len(vals) == 1 else "discrepancy",
            "detail": f"values={sorted(vals)}",
            "page": firsts[0][1].get("page"),
        })
    if len(lasts) >= 2:
        from difflib import SequenceMatcher
        from .normalize import normalize as _norm
        # Prefer OCR raw when engines disagreed (Bedrock often "autocorrects" typos)
        vals = set()
        for _, f in lasts:
            raw = f.get("textract") if f.get("status") == "manual_review" and f.get("textract") else f.get("value")
            n = _norm("name", raw) or f.get("normalized")
            if n:
                vals.add(n)
        status = "passed" if len(vals) == 1 else "discrepancy"
        if status != "passed":
            vs = list(vals)
            if len(vs) == 2 and SequenceMatcher(None, vs[0], vs[1]).ratio() >= 0.85:
                status = "warning"
        # Any engine disagreement on surname is at least a warning for the clerk
        if status == "passed" and any(f.get("status") == "manual_review" for _, f in lasts):
            status = "warning"
            vals = {str(f.get("textract")) + "|" + str(f.get("bedrock")) for _, f in lasts
                    if f.get("status") == "manual_review"}
        checks.append({
            "check": "last_name_consistent",
            "status": status,
            "detail": f"values={sorted(vals)}",
            "page": lasts[0][1].get("page"),
        })

    # Income: contract declared vs payslip net vs statement salary credit
    declared = _first(docs, "contract", "declared_monthly_income")
    payslip = _first(docs, "payslip", "net_income")
    salary = _first(docs, "bank_statement", "salary_credit")
    for label, a, b in (
        ("income_contract_vs_payslip", declared, payslip),
        ("income_contract_vs_statement", declared, salary),
        ("income_payslip_vs_statement", payslip, salary),
    ):
        if a and b and a.get("normalized") is not None and b.get("normalized") is not None:
            base = max(abs(a["normalized"]), abs(b["normalized"]), 1.0)
            ratio = abs(a["normalized"] - b["normalized"]) / base
            checks.append({
                "check": label,
                "status": "passed" if ratio <= AMOUNT_TOLERANCE else "discrepancy",
                "detail": f"{a['normalized']} vs {b['normalized']} (diff={ratio:.2%})",
                "page": a.get("page") or b.get("page"),
            })

    # Loan amount present on contract (sanity)
    loan = _first(docs, "contract", "loan_amount")
    if loan:
        checks.append({
            "check": "loan_amount_present",
            "status": "passed" if loan.get("normalized") else "missing",
            "detail": str(loan.get("normalized")),
            "page": loan.get("page"),
        })
    return checks


def completeness(docs: list[dict]) -> tuple[list[str], list[str]]:
    """Return (required_codes, missing_codes) based on the contract's required_documents."""
    required = []
    for d in docs:
        if d["doc_type"] != "contract":
            continue
        f = d["fields"].get("required_documents")
        if f and f.get("normalized"):
            required = list(f["normalized"])
            break
    if not required:
        # sensible default if contract did not list them
        required = ["id_card", "bank_statement", "payslip", "proof_of_address"]
    present = {d["doc_type"] for d in docs if d["doc_type"] != "other"}
    missing = [c for c in required if c not in present]
    return required, missing


def draft_client_message(missing: list[str], checks: list[dict], client_name: str | None) -> str | None:
    problems = [c for c in checks if c["status"] in ("discrepancy", "manual_review")]
    if not missing and not problems:
        return None
    who = client_name or "Customer"
    lines = [
        f"Dear {who},",
        "",
        "We are reviewing your loan application and need your help to complete the file:",
    ]
    if missing:
        nice = {
            "id_card": "a copy of your national ID card",
            "bank_statement": "a recent bank statement (last 3 months)",
            "payslip": "your latest payslip",
            "proof_of_address": "a proof of address (utility bill, max 3 months old)",
        }
        for m in missing:
            lines.append(f"- Please send {nice.get(m, m)}.")
    for c in problems[:5]:
        if c["check"].startswith("income_"):
            lines.append("- The income figures on your documents do not match. "
                         "Please confirm your monthly net income or send an updated payslip.")
            break
        if c["check"] == "last_name_consistent":
            lines.append("- The surname does not match across documents. "
                         "Please confirm the correct spelling.")
            break
        if c["check"] == "birth_number_valid":
            lines.append("- The birth number (rodné číslo) appears invalid. "
                         "Please re-send a clear copy of your ID.")
            break
    lines += ["", "Kind regards,", "Komerční banka – Credit Operations"]
    return "\n".join(lines)


def recommended_next_step(missing, checks, fields_statuses) -> str:
    """Priority: hard rule discrepancy > missing docs > extraction conflicts > warnings."""
    if any(c["status"] == "discrepancy" for c in checks):
        return "manual_review_discrepancy"
    if missing:
        return "request_missing_documents"
    # Field-level engine disagreements still need a clerk glance, but do not
    # block a clean package harder than a warning when no rule fired.
    n_mr = sum(1 for s in fields_statuses if s == "manual_review")
    if n_mr >= 3:
        return "manual_review_extraction"
    if n_mr >= 1 or any(s == "warning" for s in fields_statuses):
        return "spot_check_warnings"
    return "approve_ready"
