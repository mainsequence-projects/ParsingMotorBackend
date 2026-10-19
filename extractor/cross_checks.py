"""Deterministic document- and package-level checks. All thresholds come from rules.yaml."""
from __future__ import annotations

from datetime import date

from .normalize import norm_name, normalize, similarity
from .rules import parse_birth_number
from .rules_engine import Rules


def _ev(doc, field):
    f = doc["fields"].get(field) or {}
    return {"doc_id": doc["doc_id"], "doc_type": doc["doc_type"], "field": field,
            "value": f.get("value"), "page": f.get("page")}


def _first(docs, doc_type, field):
    for d in docs:
        if d["doc_type"] == doc_type:
            f = d["fields"].get(field)
            if f and f.get("normalized") is not None:
                return d, f
    return None, None


# ---------------- per document ----------------
def document_checks(doc: dict, rules: Rules, run_date: str | None = None) -> list[dict]:
    checks, fields, dt = [], doc["fields"], doc["doc_type"]
    bn = fields.get("birth_number") or {}
    if bn.get("normalized"):
        info = parse_birth_number(str(bn["normalized"]), rules.params("RC_VALID"), today=run_date)
        checks.append(rules.check("RC_VALID", "valid" if info["valid"] else "invalid",
                                  detail=info["reason"], page=bn.get("page"), doc_type=dt,
                                  evidence=[_ev(doc, "birth_number")]))
        bd = fields.get("birth_date") or {}
        if info.get("valid") and bd.get("normalized"):
            ok = bd["normalized"] == info["birth_date"]
            checks.append(rules.check(
                "RC_BIRTHDATE_MATCH", "match" if ok else "mismatch",
                detail=f"field={bd['normalized']} encoded={info['birth_date']}",
                page=bd.get("page"), doc_type=dt,
                evidence=[_ev(doc, "birth_date"), _ev(doc, "birth_number")]))
    st = fields.get("has_tax_arrears") or {}
    if dt == "tax_or_debt_certificate" and "TAX_CLEAR" in rules.rules:
        checks.append(_tax_clear(doc, st, rules))
    loan, decl = fields.get("loan_amount") or {}, fields.get("declared_monthly_income") or {}
    if loan.get("normalized") is not None and decl.get("normalized") is not None:
        eq = loan["normalized"] == decl["normalized"]
        checks.append(rules.check("CONTRACT_INCOME_NOT_LOAN", "equal" if eq else "ok",
                                  detail=f"loan={loan['normalized']} declared={decl['normalized']}",
                                  page=decl.get("page"), doc_type=dt,
                                  evidence=[_ev(doc, "loan_amount"), _ev(doc, "declared_monthly_income")]))
    return checks


def _plain(s) -> str:
    import re
    from .normalize import strip_diacritics
    return re.sub(r"\s+", " ", strip_diacritics(str(s or "")).lower()).strip()


def _tax_clear(doc, f, rules):
    """Deterministic on the reconciled yes/no value (both engines answered the same question)."""
    v = f.get("normalized")
    outcome = "clear" if v is False else "has_arrears" if v is True else "unclear"
    if f.get("status") in ("manual_review", "missing") and outcome == "clear":
        outcome = "unclear"           # engines disagreed: never call it clear on one reader
    return rules.check("TAX_CLEAR", outcome, detail=f"has_tax_arrears={v!r} ({f.get('outcome')})",
                       page=f.get("page"), doc_type=doc["doc_type"], evidence=[_ev(doc, "has_tax_arrears")])


# ---------------- package ----------------
_LEGAL_FORMS = (" s.r.o.", " s. r. o.", " sro", " a.s.", " a. s.", " as", " spol.", " k.s.", " v.o.s.")


def _company(v) -> str:
    import re
    t = " " + _plain(v)
    for lf in _LEGAL_FORMS:
        t = t.replace(lf, " ")
    return re.sub(r"[^a-z0-9]+", "", t)


def _employer_consistency(docs, rules):
    items = [(d, d["fields"]["employer"]) for d in docs
             if (d["fields"].get("employer") or {}).get("value")]
    if len(items) < 2:
        return None
    vals = [_company(f["value"]) for _, f in items]
    thr = rules.params("XDOC_EMPLOYER").get("similarity", 0.85)
    worst = min(similarity(vals[0], v) for v in vals[1:])
    return rules.check("XDOC_EMPLOYER", "consistent" if worst >= thr else "inconsistent",
                       detail=f"values={[f['value'] for _, f in items]} min_similarity={worst:.2f}",
                       page=items[0][1].get("page"), evidence=[_ev(d, "employer") for d, _ in items])


def _recency(docs, rules, run_date):
    from datetime import date as _d
    p = rules.params("DOC_RECENT")
    _, cdate = _first(docs, "contract", "contract_date")
    ref = (cdate or {}).get("normalized") or run_date or _d.today().isoformat()
    out = []
    for spec in p.get("fields") or []:
        max_age = int(spec.get("max_age_days", p.get("max_age_days", 30)))
        for d in docs:
            if d["doc_type"] != spec["doc_type"]:
                continue
            f = d["fields"].get(spec["field"]) or {}
            if not f.get("normalized"):
                outcome, detail = "undated", "issue date not read"
            else:
                age = (_d.fromisoformat(ref[:10]) - _d.fromisoformat(str(f["normalized"])[:10])).days
                # issued after the contract date is fine (documents often follow the signature)
                outcome = "recent" if age <= max_age else "stale"
                detail = f"issued={f['normalized']} ref={ref} age_days={age} max={max_age}"
            out.append(rules.check("DOC_RECENT", outcome, check=spec["check"], doc_type=d["doc_type"],
                                   detail=detail, page=f.get("page"), evidence=[_ev(d, spec["field"])]))
    return out


def _holder_matches_id(docs, rules):
    out = []
    idd, ifn = _first(docs, "id_card", "first_name")
    _, iln = _first(docs, "id_card", "last_name")
    _, irc = _first(docs, "id_card", "birth_number")
    if not idd:                                   # no ID: fall back to the contract
        idd, ifn = _first(docs, "contract", "first_name")
        _, iln = _first(docs, "contract", "last_name")
        _, irc = _first(docs, "contract", "birth_number")
    if not idd or not ifn or not iln:
        return out
    ref = (ifn["normalized"] or "") + (iln["normalized"] or "")
    near = rules.params("XDOC_HOLDER_RC").get("near_miss_ratio", 0.85)
    for d in docs:
        if d["doc_type"] != "tax_or_debt_certificate":
            continue
        hn = d["fields"].get("holder_name") or {}
        rc = d["fields"].get("birth_number") or {}
        if not hn.get("normalized"):
            continue
        name_ok = hn["normalized"] == ref
        rc_ok = (not rc.get("normalized") or not irc or str(rc["normalized"]) == str(irc["normalized"]))
        if name_ok and rc_ok:
            outcome = "consistent"
        elif rc_ok and similarity(hn["normalized"], ref) >= near:
            outcome = "near_miss"
        else:
            outcome = "inconsistent"
        out.append(rules.check(
            "XDOC_HOLDER_RC", outcome, doc_type=d["doc_type"],
            detail=f"holder={hn['normalized']} rc={rc.get('normalized')} vs {idd['doc_type']} {ref} rc={(irc or {}).get('normalized')}",
            page=hn.get("page"),
            evidence=[_ev(d, "holder_name"), _ev(d, "birth_number"), _ev(idd, "first_name"),
                      _ev(idd, "last_name"), _ev(idd, "birth_number")]))
    return out

def _name_consistency(rules, rule_id, docs, field):
    items = [(d, d["fields"][field]) for d in docs
             if (d["fields"].get(field) or {}).get("normalized")]
    if len(items) < 2:
        return None
    # When engines disagreed, compare the OCR reading too (the LLM may 'autocorrect' typos).
    vals = set()
    for _, f in items:
        vals.add(f["normalized"])
        if f.get("outcome") == "disagree" and f.get("textract"):
            vals.add(norm_name(f["textract"]))
    vals.discard(None)
    evidence = [_ev(d, field) for d, _ in items]
    if len(vals) == 1:
        outcome = "consistent"
        if rule_id == "XDOC_LAST_NAME" and any(f.get("outcome") == "disagree" for _, f in items):
            outcome = "engine_disagreement"
    else:
        vs = sorted(vals)
        near = rules.params(rule_id).get("near_miss_ratio")
        if near is not None and len(vs) == 2 and similarity(vs[0], vs[1]) >= near:
            outcome = "near_miss"
        else:
            outcome = "inconsistent"
    return rules.check(rule_id, outcome, detail=f"values={sorted(vals)}",
                       page=items[0][1].get("page"), evidence=evidence)


def package_checks(docs: list[dict], rules: Rules, run_date: str | None = None) -> list[dict]:
    checks = []

    # birth number across docs
    bns = [(d, d["fields"]["birth_number"]) for d in docs
           if (d["fields"].get("birth_number") or {}).get("normalized")]
    if len(bns) >= 2:
        vals = {str(f["normalized"]) for _, f in bns}
        checks.append(rules.check("XDOC_BIRTH_NUMBER",
                                  "consistent" if len(vals) == 1 else "inconsistent",
                                  detail=f"values={sorted(vals)}", page=bns[0][1].get("page"),
                                  evidence=[_ev(d, "birth_number") for d, _ in bns]))

    for rid, field in (("XDOC_FIRST_NAME", "first_name"), ("XDOC_LAST_NAME", "last_name")):
        c = _name_consistency(rules, rid, docs, field)
        if c:
            checks.append(c)

    # holder / full name vs contract first+last
    cd, cfn = _first(docs, "contract", "first_name")
    _, cln = _first(docs, "contract", "last_name")
    if cd and cfn and cln:
        ref = (cfn["normalized"] or "") + (cln["normalized"] or "")
        near = rules.params("XDOC_FULL_NAME")["near_miss_ratio"]
        holder_fields = rules.params("XDOC_FULL_NAME").get("holder_fields") or ["account_holder", "full_name"]
        for d in docs:
            for fld in holder_fields:
                f = d["fields"].get(fld) or {}
                if not f.get("normalized"):
                    continue
                n = f["normalized"]
                if n == ref:
                    outcome = "consistent"
                elif similarity(n, ref) >= near:
                    outcome = "near_miss"
                else:
                    outcome = "inconsistent"
                checks.append(rules.check("XDOC_FULL_NAME", outcome, doc_type=d["doc_type"],
                                          detail=f"{n} vs contract {ref}", page=f.get("page"),
                                          evidence=[_ev(d, fld), _ev(cd, "first_name"), _ev(cd, "last_name")]))

    # address
    addrs = [(d, d["fields"]["address"]) for d in docs
             if (d["fields"].get("address") or {}).get("normalized")]
    if len(addrs) >= 2:
        ref = addrs[0][1]["normalized"]
        thr = rules.params("XDOC_ADDRESS")["similarity"]
        worst = min(similarity(ref, f["normalized"]) for _, f in addrs[1:])
        checks.append(rules.check("XDOC_ADDRESS", "consistent" if worst >= thr else "inconsistent",
                                  detail=f"min_similarity={worst:.2f} (threshold {thr})",
                                  page=addrs[0][1].get("page"),
                                  evidence=[_ev(d, "address") for d, _ in addrs]))

    # income pairs
    ip = rules.params("XDOC_INCOME")
    for pair in ip["pairs"]:
        (ta, fa), (tb, fb) = pair["a"].split("."), pair["b"].split(".")
        da, a = _first(docs, ta, fa)
        db, b = _first(docs, tb, fb)
        if a and b:
            base = max(abs(a["normalized"]), abs(b["normalized"]), 1.0)
            diff = abs(a["normalized"] - b["normalized"]) / base
            checks.append(rules.check(
                "XDOC_INCOME", "within_tolerance" if diff <= ip["tolerance"] else "out_of_tolerance",
                check=pair["check"],
                detail=f"{a['normalized']} vs {b['normalized']} (diff={diff:.2%}, tol={ip['tolerance']:.0%})",
                page=a.get("page"), evidence=[_ev(da, fa), _ev(db, fb)]))

    if "XDOC_EMPLOYER" in rules.rules:
        c = _employer_consistency(docs, rules)
        if c:
            checks.append(c)
    if "DOC_RECENT" in rules.rules:
        checks.extend(_recency(docs, rules, run_date))
    if "XDOC_HOLDER_RC" in rules.rules:
        checks.extend(_holder_matches_id(docs, rules))

    # loan amount present
    for d in docs:
        if d["doc_type"] == "contract":
            f = d["fields"].get("loan_amount") or {}
            checks.append(rules.check("LOAN_AMOUNT_PRESENT",
                                      "present" if f.get("normalized") else "absent",
                                      detail=str(f.get("normalized")), page=f.get("page"),
                                      evidence=[_ev(d, "loan_amount")]))
            break

    # ID expiry vs contract date (or run date)
    _, cdate = _first(docs, "contract", "contract_date")
    ref_date = (cdate or {}).get("normalized") or run_date or date.today().isoformat()
    for d in docs:
        if d["doc_type"] == "id_card":
            f = d["fields"].get("expiry_date") or {}
            if f.get("normalized"):
                ok = f["normalized"] >= ref_date
                checks.append(rules.check("ID_NOT_EXPIRED", "valid" if ok else "expired",
                                          doc_type="id_card",
                                          detail=f"expiry={f['normalized']} ref={ref_date}",
                                          page=f.get("page"), evidence=[_ev(d, "expiry_date")]))
    return checks


def completeness(docs: list[dict], rules: Rules) -> tuple[list[str], list[str], str]:
    """(required, missing, source) — source is 'contract' or 'default'."""
    required, source = [], "default"
    for d in docs:
        if d["doc_type"] == "contract":
            f = d["fields"].get("required_documents") or {}
            if f.get("normalized"):
                required, source = list(f["normalized"]), "contract"
                break
    if not required:
        required = list(rules.params("COMPLETENESS")["default_required"])
    # The contract defines the package: without it there is nothing to check against.
    for t in reversed(rules.params("COMPLETENESS").get("always_required") or []):
        if t not in required:
            required.insert(0, t)
    from .config import DOC_TYPES
    present = {d["doc_type"] for d in docs}
    # unknown types are reported by the COMPLETENESS check, never sent to the client as-is
    return required, [c for c in required if c not in present and c in DOC_TYPES], source


def page_gaps(docs: list[dict]) -> list[dict]:
    gaps = []
    for d in docs:
        nums = [p.get("page_number") for p in d.get("pages", []) if p.get("page_number")]
        totals = [p.get("page_count") for p in d.get("pages", []) if p.get("page_count")]
        if not totals:
            continue
        total = max(totals)
        missing = [n for n in range(1, total + 1) if n not in nums]
        if missing:
            gaps.append({"doc_id": d["doc_id"], "doc_type": d["doc_type"],
                         "page_count": total, "missing_page_numbers": missing})
    return gaps


def completeness_checks(docs, rules, required, missing, source, gaps):
    from .config import DOC_TYPES
    unknown = [c for c in required if c not in DOC_TYPES]
    sev = rules.rule("COMPLETENESS")["severity"]
    if unknown and "unrecognised_type" in sev:
        outcome = "unrecognised_type"
    else:
        outcome = "incomplete" if missing else "complete"
    checks = [rules.check("COMPLETENESS", outcome,
                          detail=f"required({source})={required} missing={missing}"
                                 + (f" unrecognised={unknown}" if unknown else ""))]
    contracts = [d for d in docs if d["doc_type"] == "contract"]
    if "CONTRACT_SINGLE" in rules.rules and len(contracts) > 1:   # optional: absent in v0.2 audit rules
        checks.append(rules.check(
            "CONTRACT_SINGLE", "multiple",
            detail=f"{len(contracts)} contracts: {[d['doc_id'] for d in contracts]}; using {contracts[0]['doc_id']}",
            page=contracts[1]["pages"][0]["page"] if contracts[1].get("pages") else None,
            evidence=[{"doc_id": d["doc_id"], "doc_type": "contract", "field": "contract_number",
                       "value": (d["fields"].get("contract_number") or {}).get("value"),
                       "page": (d["fields"].get("contract_number") or {}).get("page")} for d in contracts]))
    elif "CONTRACT_SINGLE" in rules.rules and contracts:
        checks.append(rules.check("CONTRACT_SINGLE", "single", detail=contracts[0]["doc_id"]))
    has_markers = any(p.get("page_count") for d in docs for p in d.get("pages", []))
    if not has_markers:
        checks.append(rules.check("PAGE_CONTINUITY", "no_markers", detail="no 'page x of y' markers"))
    else:
        checks.append(rules.check("PAGE_CONTINUITY", "gap" if gaps else "complete",
                                  detail=f"gaps={gaps}" if gaps else "all pages present"))
    ignore = set(rules.params("UNEXPECTED_DOCUMENTS").get("ignore_types") or [])
    extra = sorted({d["doc_type"] for d in docs} - set(required) - ignore)
    checks.append(rules.check("UNEXPECTED_DOCUMENTS", "present" if extra else "none",
                              detail=f"unrequested={extra}"))
    return checks, extra


def recommended_next_step(rules: Rules, checks, field_statuses, missing, gaps) -> str:
    n_mr = sum(1 for s in field_statuses if s == "manual_review")
    statuses = [c["status"] for c in checks] + list(field_statuses)
    conds = {
        "any_check_discrepancy": any(c["status"] == "discrepancy" for c in checks),
        "missing_documents_or_pages": bool(missing) or bool(gaps),
        "many_field_manual_review": n_mr >= rules.next_step["params"]["manual_review_extraction_min_fields"],
        "any_warning_or_manual_review": any(s in ("warning", "manual_review") for s in statuses),
        "any_field_missing": any(s == "missing" for s in field_statuses),
        "always": True,
    }
    for item in rules.next_step["order"]:
        if conds[item["when"]]:
            return item["step"]
    return "approve_ready"


NICE = {
    "en": {
        "contract": "the signed loan contract (it defines which documents we need)",
        "id_card": "a copy of your national ID card (občanský průkaz)",
        "bank_statement": "a recent bank statement for the last 3 months (výpis z účtu)",
        "payslip": "your latest payslip (výplatní lístek)",
        "proof_of_address": "a proof of address (utility bill, max 3 months old)",
        "employer_confirmation": "an employer confirmation of employment and income (potvrzení o zaměstnání a příjmu), max 30 days old",
        "tax_or_debt_certificate": "a certificate of no tax arrears from the tax office (potvrzení o bezdlužnosti), max 30 days old",
    },
    "cs": {
        "contract": "podepsanou úvěrovou smlouvu (určuje, které dokumenty potřebujeme)",
        "id_card": "kopii občanského průkazu",
        "bank_statement": "aktuální výpis z účtu za poslední 3 měsíce",
        "payslip": "nejnovější výplatní lístek",
        "proof_of_address": "doklad o bydlišti (faktura za energie, max. 3 měsíce starý)",
        "employer_confirmation": "potvrzení o zaměstnání a výši příjmu (max. 30 dnů staré)",
        "tax_or_debt_certificate": "potvrzení o bezdlužnosti od Finančního úřadu (max. 30 dnů staré)",
    },
}

_DOC_LABEL = {
    "en": {"bank_statement": "bank statement", "payslip": "payslip", "id_card": "ID card",
           "proof_of_address": "proof of address", "employer_confirmation": "employer confirmation",
           "tax_or_debt_certificate": "tax clearance certificate", "contract": "contract"},
    "cs": {"bank_statement": "výpis z účtu", "payslip": "výplatní lístek", "id_card": "občanský průkaz",
           "proof_of_address": "doklad o bydlišti", "employer_confirmation": "potvrzení o zaměstnání",
           "tax_or_debt_certificate": "potvrzení o bezdlužnosti", "contract": "smlouva"},
}


def _problem_lines(lang, said_rules, problems):
    """One bullet per rule_id, bilingual templates. said_rules is mutated."""
    out = []
    for c in problems:
        rid = c["rule_id"]
        if rid in said_rules:
            continue
        said_rules.add(rid)
        if rid == "XDOC_INCOME":
            out.append({"en": "The income figures on your documents do not match. Please confirm your "
                              "monthly net income or send an updated payslip / employer confirmation.",
                        "cs": "Údaje o příjmu na Vašich dokumentech se neshodují. Prosíme o potvrzení "
                              "měsíčního čistého příjmu nebo zaslání aktualizovaného výplatního lístku / "
                              "potvrzení o zaměstnání."}[lang])
        elif rid == "XDOC_EMPLOYER":
            out.append({"en": "The employer name on your payslip does not match the employer confirmation. "
                              "Please send matching documents.",
                        "cs": "Název zaměstnavatele na výplatním lístku se neshoduje s potvrzením o "
                              "zaměstnání. Prosíme o zaslání odpovídajících dokladů."}[lang])
        elif rid in ("XDOC_LAST_NAME", "XDOC_FIRST_NAME", "XDOC_FULL_NAME", "XDOC_HOLDER_RC"):
            out.append({"en": "Your name (or birth number) does not match across documents. Please confirm "
                              "the correct spelling and re-send a clear copy of your ID.",
                        "cs": "Vaše jméno (nebo rodné číslo) se na dokumentech neshoduje. Prosíme o "
                              "potvrzení správného zápisu a zaslání čitelné kopie občanského průkazu."}[lang])
        elif rid in ("RC_VALID", "XDOC_BIRTH_NUMBER", "RC_BIRTHDATE_MATCH"):
            out.append({"en": "The birth number (rodné číslo) on your documents appears invalid or "
                              "inconsistent. Please re-send a clear copy of your ID.",
                        "cs": "Rodné číslo na Vašich dokumentech se jeví neplatné nebo nekonzistentní. "
                              "Prosíme o zaslání čitelné kopie občanského průkazu."}[lang])
        elif rid == "ID_NOT_EXPIRED":
            out.append({"en": "Your ID card appears to be expired. Please send a valid ID.",
                        "cs": "Váš občanský průkaz je zřejmě propadlý. Prosíme o zaslání platného dokladu."}[lang])
        elif rid == "DOC_RECENT":
            out.append({"en": "One of the required certificates is older than 30 days. Please send a "
                              "freshly issued employer confirmation and/or tax clearance certificate.",
                        "cs": "Jeden z požadovaných dokladů je starší než 30 dnů. Prosíme o aktuální "
                              "potvrzení o zaměstnání a/nebo potvrzení o bezdlužnosti."}[lang])
        elif rid == "TAX_CLEAR":
            out.append({"en": "The tax office certificate indicates outstanding arrears. Please resolve "
                              "them and send an updated certificate of no tax arrears.",
                        "cs": "Potvrzení Finančního úřadu uvádí nedoplatky. Prosíme o jejich vyřešení a "
                              "zaslání aktualizovaného potvrzení o bezdlužnosti."}[lang])
    return out


def _draft(lang, missing, gaps, problems, client_name) -> str:
    greet = {"en": f"Dear {client_name or 'Customer'},",
             "cs": f"Vážená/ý {client_name or 'kliente/klientko'},"}[lang]
    intro = {"en": "We are reviewing your loan application and need your help to complete the file:",
             "cs": "Posuzujeme Vaši žádost o úvěr a potřebujeme Vaši součinnost k doplnění spisu:"}[lang]
    lines = [greet, "", intro]
    for m in missing:
        ask = {"en": f"- Please send {NICE['en'].get(m, m)}.",
               "cs": f"- Prosíme o zaslání: {NICE['cs'].get(m, m)}."}[lang]
        lines.append(ask)
    for g in gaps:
        label = _DOC_LABEL[lang].get(g["doc_type"], g["doc_type"].replace("_", " "))
        pages = ", ".join(str(n) for n in g["missing_page_numbers"])
        lines.append({"en": f"- Your {label} is incomplete: page(s) {pages} of {g['page_count']} "
                            "are missing. Please send the complete document.",
                      "cs": f"- Váš dokument ({label}) je neúplný: chybí strana/strany {pages} "
                            f"z {g['page_count']}. Prosíme o zaslání kompletního dokladu."}[lang])
    lines.extend(_problem_lines(lang, set(), problems))
    sign = {"en": ["", "Kind regards,", "Komerční banka – Credit Operations"],
            "cs": ["", "S pozdravem,", "Komerční banka – Úvěrové operace"]}[lang]
    return "\n".join(lines + sign)


def draft_client_message(missing, gaps, checks, client_name) -> str | None:
    """Back-compat: English body (same string as before for existing tests / demos)."""
    msgs = draft_client_messages(missing, gaps, checks, client_name)
    return None if msgs is None else msgs["en"]


def draft_client_messages(missing, gaps, checks, client_name) -> dict | None:
    """Bilingual drafts for Jose's UI. None when there is nothing to ask the client."""
    problems = [c for c in checks if c["status"] == "discrepancy"]
    if not missing and not gaps and not problems:
        return None
    return {"en": _draft("en", missing, gaps, problems, client_name),
            "cs": _draft("cs", missing, gaps, problems, client_name)}


def model_checks(docs: list[dict], raw_docs: list[dict], rules: Rules) -> list[dict]:
    """Flag any page/document that was not read by the configured primary model."""
    if "MODEL_PRIMARY" not in rules.rules:
        return []
    want = rules.params("MODEL_PRIMARY")
    used = []
    for rd in raw_docs:
        for pg in rd.get("pages") or []:
            if pg.get("model") and pg["model"] != want.get("classification"):
                used.append({"doc_id": rd["doc_id"], "stage": "classification", "page": pg.get("page"),
                             "model": pg["model"]})
        models = {v.get("model") for v in (rd["raw"].get("bedrock") or {}).values()
                  if isinstance(v, dict) and v.get("model")}
        for m in sorted(models - {want.get("extraction")}):
            used.append({"doc_id": rd["doc_id"], "stage": "extraction", "model": m})
    return [rules.check("MODEL_PRIMARY", "fallback" if used else "primary",
                        detail=f"fallback models used: {used}" if used else "primary models only",
                        evidence=used)]
