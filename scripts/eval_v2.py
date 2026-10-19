"""Evaluate the V2 parser (/classify_json + /extract_typed_json) on samples/NN_*/ground_truth.json.

  python scripts/eval_v2.py --mock                 # no AWS: oracle replies; checks the harness
                                                   # and estimates the real cost from real prompts
  python scripts/eval_v2.py --real --budget 6      # real Bedrock + Textract, eu-central-1

Measures READING only (Back Office decides acceptance): page types, grouping, final draft
values vs ground truth, reader agreement, and how many wrong values were flagged
(readers disagree / single reader) instead of passing silently ("agree" but wrong).
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extractor import aws, document_service, ocr_reader  # noqa: E402
from extractor.normalize import norm_text  # noqa: E402
from extractor.splitter import group_documents  # noqa: E402

CONTRACTS = {c["document_type"]: c for c in json.loads(
    (ROOT / "tests/fixtures/backoffice_canonical_contracts.json").read_text())}
# Old engine type -> Back Office type, and ground-truth field -> draft pointer.
# "full_name" joins first+last; "address" compares the joined address parts.
MAP = {
    "contract": ("loan_contract", {
        "contract_number": "/contract_number", "name": ("full_name", "/full_name"),
        "birth_number": "/borrower_identifier", "address": ("address", "/borrower_address"),
        "loan_amount": "/loan_amount/amount", "declared_monthly_income": "/declared_monthly_net_income/amount",
        "contract_date": "/issue_date"}),
    "id_card": ("national_identity_card", {
        "first_name": "/given_names", "last_name": "/last_name", "birth_date": "/date_of_birth",
        "document_number": "/identity_number", "expiry_date": "/expiration_date"}),
    "bank_statement": ("bank_statement", {
        "account_holder": "/account_holders/0", "iban": "/account_identifier",
        "period_from": "/statement_period/start_date", "period_to": "/statement_period/end_date",
        "closing_balance": "/closing_balance/amount"}),
    "payslip": ("payslip", {
        "name": ("full_name", "/employee_name"), "birth_number": "/employee_identifier",
        "employer": "/employer/name", "net_income": "/net_pay/amount"}),
    "proof_of_address": ("utility_bill", {
        "full_name": "/account_holder_name", "address": ("address", "/service_address"),
        "issuer": "/provider_name", "issue_date": "/issue_date"}),
    "employer_confirmation": ("employer_confirmation", {
        "employee_name": "/employee_name", "birth_number": "/employee_identifier",
        "employer": "/employer/name", "employer_ico": "/employer/registration_number",
        "position": "/position", "employment_start": "/employment_start_date",
        "contract_type": "/contract_term_as_printed", "monthly_net_income": "/monthly_net_income/amount",
        "issue_date": "/issue_date"}),
    "tax_or_debt_certificate": ("tax_clearance_certificate", {
        "holder_name": "/taxpayer_name", "birth_number": "/tax_identifier",
        "tax_office": "/issuing_authority", "certificate_number": "/certificate_number",
        "issue_date": "/issue_date"}),
}
ADDRESS_SIMILARITY = 0.85
IMAGE_TOKENS = 1550          # ~1.15 MP image after Bedrock resizing
CHARS_PER_TOKEN = 3.5
PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0)}   # USD / 1M tokens, Claude 4.5 list prices
TEXTRACT_PAGE_USD = 0.015                               # AnalyzeDocument QUERIES, per call


def bo_type(old: str) -> str | None:
    return MAP.get(old, (None,))[0]


def expected_value(fields: dict, spec):
    if isinstance(spec, tuple) and spec[0] == "full_name" and "first_name" in fields:
        return f"{fields['first_name']} {fields['last_name']}"
    return None


def resolve_gt(old_type: str, gt_fields: dict):
    """[(label, kind, pointer, expected)] for one document type."""
    out = []
    for label, spec in MAP[old_type][1].items():
        kind, pointer = spec if isinstance(spec, tuple) else ("value", spec)
        exp = expected_value(gt_fields, spec) if label == "name" else gt_fields.get(label)
        if exp is not None:
            out.append((label, kind, pointer, exp))
    return out


def get(attrs, pointer):
    node = attrs
    for part in pointer.lstrip("/").split("/"):
        if isinstance(node, list):
            node = node[int(part)] if part.isdigit() and int(part) < len(node) else None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def joined_address(v) -> str | None:
    if not isinstance(v, dict):
        return v
    parts = [v.get(k) for k in ("address_line_1", "address_line_2", "postal_code", "city")]
    return " ".join(str(p) for p in parts if p) or None


def correct(schema, kind, pointer, expected, got) -> bool:
    if got is None:
        return False
    if kind == "address":
        a, b = norm_text(joined_address(got)), norm_text(expected)
        return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= ADDRESS_SIMILARITY
    leaf = ocr_reader.leaf_schema(schema, pointer)
    k = ocr_reader.kind(leaf, pointer)
    if k in ("amount", "date", "id", "currency"):
        return ocr_reader.compare_form(leaf, pointer, got) == ocr_reader.compare_form(leaf, pointer, expected)
    # Text is strict on diacritics: the draft value is what the reviewer sees.
    return re.sub(r"\s+", " ", str(got)).strip().casefold() == re.sub(r"\s+", " ", str(expected)).strip().casefold()


# ---------------- oracle providers (--mock) ----------------
class Oracle:
    """Replies built from the ground truth; records every call to estimate the real cost."""

    def __init__(self):
        self.calls, self.gt, self.page_types = [], None, []

    def start(self, gt):
        self.gt, self.file, self.page_i = gt, None, 0
        self.page_info = {}
        for d in gt["expected_documents"]:
            for i, p in enumerate(d["pages"]):
                self.page_info[(d["source_file"], p)] = (d["doc_type"], i + 1, len(d["pages"]))

    def converse(self, prompt, images=(), model_id=None, **kw):
        model = model_id or "sonnet"
        if prompt.startswith("Classify ONLY"):
            self.page_i += 1     # V2 classifies one file per request, pages in order
            old, num, count = self.page_info.get((self.file, self.page_i), ("other", None, None))
            reply = {"document_type": bo_type(old), "confidence": 0.95, "reason": "oracle",
                     "printed_page_number": num if count and count > 1 else None,
                     "printed_page_count": count if count and count > 1 else None,
                     "group_hint": None, "boundary": "starts_document" if num == 1 else "continues_document"}
            kind = "classify"
        else:
            doc_type = re.search(r"explicit type '([a-z_]+)'", prompt).group(1)
            pages = json.loads(re.search(r"physical page order: (\[[^\]]*\])", prompt).group(1))
            reply = self.reading(doc_type, pages)
            kind = "extract"
        self.calls.append({"kind": kind, "model": model, "images": len(images), "prompt_chars": len(prompt),
                           "reply_chars": len(json.dumps(reply, ensure_ascii=False))})
        return reply, model, {"cache_hit": False, "usage": {}}

    def reading(self, doc_type, pages):
        old = next(o for o, (n, _) in MAP.items() if n == doc_type)
        attrs = {"document_type": doc_type}
        for label, kind, pointer, exp in resolve_gt(old, self.gt["expected_fields"].get(old, {})):
            if kind == "address":
                street, _, rest = str(exp).partition(", ")
                m = re.match(r"(\d{3} ?\d{2}) (.+)", rest)
                put(attrs, pointer, {"address_line_1": street, "postal_code": m.group(1) if m else None,
                                     "city": m.group(2) if m else rest})
            elif pointer.endswith("/amount"):
                put(attrs, pointer, f"{float(exp):.2f}")
                put(attrs, pointer.rsplit("/", 1)[0] + "/currency", "CZK")
            elif pointer.endswith("/0"):
                put(attrs, pointer[:-2], [str(exp)])
            else:
                put(attrs, pointer, str(exp))
        cites = [{"pointer": p, "pages": [pages[0]], "raw_text": str(v)} for p, v in leaves(attrs)]
        return {"attributes": attrs, "citations": cites}

    def textract(self, png, queries):
        self.calls.append({"kind": "textract", "queries": len(queries)})
        return {}, {"cache_hit": False, "called": True}


def put(attrs, pointer, value):
    parts = pointer.lstrip("/").split("/")
    node = attrs
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def leaves(value, pointer=""):
    if isinstance(value, dict):
        for k, v in value.items():
            if pointer + "/" + k != "/document_type":
                yield from leaves(v, pointer + "/" + k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, f"{pointer}/{i}")
    elif value is not None:
        yield pointer, value


def estimate_usd(calls) -> dict:
    out = {"bedrock_usd": 0.0, "textract_usd": 0.0, "calls": {"classify": 0, "extract": 0, "textract_pages": 0}}
    for c in calls:
        if c["kind"] == "textract":
            pages = math.ceil(c["queries"] / 15)
            out["calls"]["textract_pages"] += pages
            out["textract_usd"] += pages * TEXTRACT_PAGE_USD
            continue
        out["calls"][c["kind"]] += 1
        inp = c["prompt_chars"] / CHARS_PER_TOKEN + IMAGE_TOKENS * c["images"]
        outp = 120 if c["kind"] == "classify" else c["reply_chars"] / 3.0
        price = PRICES["haiku" if c["kind"] == "classify" else "sonnet"]
        out["bedrock_usd"] += (inp * price[0] + outp * price[1]) / 1e6
    out["total_usd"] = round(out["bedrock_usd"] + out["textract_usd"], 4)
    out["bedrock_usd"], out["textract_usd"] = round(out["bedrock_usd"], 4), round(out["textract_usd"], 4)
    return out


# ---------------- one package ----------------
CATALOG = [{"document_type": t, "description": c["canonical_schema"].get("description") or t}
           for t, c in CONTRACTS.items()]


def files_for(pkg: Path, names):
    return [{"filename": n, "content_base64": base64.b64encode((pkg / n).read_bytes()).decode()} for n in names]


def run_package(client, pkg: Path, reader_mode: str, oracle: Oracle | None = None) -> dict:
    gt = json.loads((pkg / "ground_truth.json").read_text())
    pages = []
    for name in gt["inputs"]:          # V2 takes one file per request
        if oracle:
            oracle.file, oracle.page_i = name, 0
        r = client.post("/classify_json", json={"files": files_for(pkg, [name]), "document_types": CATALOG,
                                                 "contract_version": 2})
        r.raise_for_status()
        pages += r.json()["pages"]
    exp_type = {(d["source_file"], n): bo_type(d["doc_type"]) for d in gt["expected_documents"] for n in d["pages"]}
    page_ok = sum(1 for p in pages if p["document_type"] == exp_type.get((p["source_file"], p["page"])))
    groups = group_documents([{"source_file": p["source_file"], "page": p["page"], "doc_type": p["document_type"],
                               "doc_key": p.get("group_hint"), "page_number": p.get("printed_page_number"),
                               "confidence": p["confidence"], "reason": p.get("reason", "")} for p in pages])
    got_groups = sorted((g["source_file"], g["doc_type"] or "none", tuple(p["page"] for p in g["pages"])) for g in groups)
    exp_groups = sorted((d["source_file"], bo_type(d["doc_type"]) or "none", tuple(d["pages"]))
                        for d in gt["expected_documents"])
    docs = {}
    for g in groups:
        t = g["doc_type"]
        if t not in CONTRACTS or t in docs:
            continue
        c = CONTRACTS[t]
        body = {"files": files_for(pkg, [g["source_file"]]), "document_type": t, "schema_version": c["version"],
                "schema_hash": c["content_sha256"], "canonical_schema": c["canonical_schema"],
                "output_schema": c["draft_schema"], "contract_version": 2, "reader_mode": reader_mode,
                "page_selection": [p["page"] for p in g["pages"]]}
        resp = client.post("/extract_typed_json", json=body)
        docs[t] = resp.json() if resp.status_code == 200 else {"error": resp.status_code, "detail": resp.text[:300]}
    fields = []
    for old, gt_fields in gt["expected_fields"].items():
        t = bo_type(old)
        if t is None:
            continue
        doc = docs.get(t) or {}
        evidence = {f["pointer"]: f for f in doc.get("field_evidence") or []}
        for label, kind, pointer, exp in resolve_gt(old, gt_fields):
            got = get(doc.get("attributes") or {}, pointer)
            ev = evidence.get(pointer) or next((v for k, v in evidence.items() if k.startswith(pointer + "/")), {})
            ok = correct(CONTRACTS[t]["draft_schema"], kind, pointer, exp, got)
            fields.append({"doc": old, "field": label, "pointer": pointer, "expected": exp, "got": got, "ok": ok,
                           "agreement": ev.get("agreement"), "selected": ev.get("selected_reader_id")})
    return {"package_id": gt["package_id"], "pages": len(pages), "pages_ok": page_ok,
            "grouping_ok": got_groups == exp_groups, "documents_extracted": len(docs),
            "extract_errors": [t for t, d in docs.items() if "error" in d], "fields": fields}


def summarize(results) -> dict:
    f = [x for r in results for x in r["fields"]]
    wrong = [x for x in f if not x["ok"]]
    agree = {}
    for x in f:
        agree[x["agreement"]] = agree.get(x["agreement"], 0) + 1
    return {"packages": len(results),
            "pages": f"{sum(r['pages_ok'] for r in results)}/{sum(r['pages'] for r in results)}",
            "grouping": f"{sum(r['grouping_ok'] for r in results)}/{len(results)}",
            "fields": f"{len(f) - len(wrong)}/{len(f)}",
            "field_accuracy": round((len(f) - len(wrong)) / len(f), 4) if f else None,
            "wrong_flagged": sum(1 for x in wrong if x["agreement"] in ("disagree", "single_reader", "missing")),
            "wrong_silent": sum(1 for x in wrong if x["agreement"] == "agree"),
            "agreement": agree,
            "extract_errors": {r["package_id"]: r["extract_errors"] for r in results if r["extract_errors"]}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="oracle replies, no AWS, cost estimate")
    mode.add_argument("--real", action="store_true", help="real Bedrock + Textract calls")
    ap.add_argument("--budget", type=float, help="hard USD cap for --real (required)")
    ap.add_argument("--samples", default=str(ROOT / "samples"))
    ap.add_argument("--only", nargs="*", help="package id prefixes, e.g. 05 13")
    ap.add_argument("--reader-mode", default="independent_readers", choices=["single", "independent_readers"])
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    if a.real and not a.budget:
        ap.error("--real needs --budget")
    import os
    if a.real:
        os.environ["EXTRACTOR_BUDGET_USD"] = str(a.budget)
    from fastapi.testclient import TestClient

    from extractor import api
    api.API_KEY = ""
    oracle = Oracle() if a.mock else None
    if oracle:
        def no_aws(*args, **kwargs):
            raise RuntimeError("--mock must not reach AWS")
        aws._client = no_aws
        document_service.converse_json = oracle.converse
        aws.textract_queries = oracle.textract
    client = TestClient(api.app)
    pkgs = sorted(p for p in Path(a.samples).glob("[0-9][0-9]_*") if (p / "ground_truth.json").exists())
    if a.only:
        pkgs = [p for p in pkgs if any(p.name.startswith(o) for o in a.only)]
    results = []
    for pkg in pkgs:
        if oracle:
            oracle.start(json.loads((pkg / "ground_truth.json").read_text()))
        results.append(run_package(client, pkg, a.reader_mode, oracle))
        r = results[-1]
        print(f"{r['package_id']:<34} pages {r['pages_ok']}/{r['pages']}  grouping {'ok' if r['grouping_ok'] else 'NO'}"
              f"  fields {sum(x['ok'] for x in r['fields'])}/{len(r['fields'])}", file=sys.stderr)
    out = {"mode": "mock" if a.mock else "real", "reader_mode": a.reader_mode, "summary": summarize(results),
           "packages": results}
    if oracle:
        out["estimated_real_cost"] = estimate_usd(oracle.calls)
    else:
        from extractor import cost
        out["cost"] = cost.snapshot() if hasattr(cost, "snapshot") else None
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"summary": out["summary"], "estimated_real_cost": out.get("estimated_real_cost")},
                     indent=2, ensure_ascii=False))
    return out


if __name__ == "__main__":
    main()
