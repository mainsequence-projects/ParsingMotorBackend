"""Dual extraction: Textract QUERIES + Bedrock Converse, run in parallel."""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from .aws import converse_json, textract_queries
from .config import MAX_WORKERS, SCHEMAS

SYSTEM = (
    "You extract structured fields from bank KYC / loan documents. "
    "Return ONLY JSON. Use null when a field is not present. Never invent values."
)


def _field_defs(doc_type: str) -> dict:
    return SCHEMAS.get(doc_type, {}).get("fields") or {}


def _textract_for_doc(doc: dict) -> dict:
    """Run Textract on every page; keep best-confidence answer per field."""
    fields = _field_defs(doc["doc_type"])
    queries = {k: f["query"] for k, f in fields.items() if f.get("textract", True) and f.get("query")}
    if not queries:
        return {}
    best: dict = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(doc["pages"]))) as pool:
        futs = {pool.submit(textract_queries, p["png"], queries): p["page"] for p in doc["pages"]}
        for fut in as_completed(futs):
            page = futs[fut]
            for alias, ans in fut.result().items():
                if alias not in best or ans["confidence"] > best[alias]["confidence"]:
                    best[alias] = {**ans, "page": page}
    return best


def _bedrock_for_doc(doc: dict) -> dict:
    fields = _field_defs(doc["doc_type"])
    if not fields:
        return {}
    schema_hint = {}
    for name, meta in fields.items():
        if meta.get("type") == "doc_list":
            schema_hint[name] = ["id_card", "bank_statement", "payslip", "proof_of_address"]
        elif meta.get("type") == "amount":
            schema_hint[name] = 0.0
        else:
            schema_hint[name] = "string or null"
    prompt = (
        f"This document is of type '{doc['doc_type']}'. Extract the fields below.\n"
        f"Return ONLY a JSON object with exactly these keys (value or null):\n"
        f"{json.dumps(schema_hint, indent=2)}\n"
        "Rules:\n"
        "- Dates as YYYY-MM-DD when possible, else keep the original string.\n"
        "- Amounts as numbers (no currency symbol). For declared_monthly_income use ONLY the figure next to that label, never the loan amount.\n"
        "- birth_number (rodne cislo) digits with optional slash, exactly as printed.\n"
        "- For required_documents, map free text to the codes: id_card, bank_statement, "
        "payslip, proof_of_address. Only include those that are listed as required.\n"
        "- Do not invent. Missing => null (or [] for lists)."
    )
    images = [p["png"] for p in doc["pages"][:6]]  # cost guard
    data, model = converse_json(prompt, images=images, system=SYSTEM, max_tokens=1500)
    out = {}
    for k in fields:
        out[k] = {"value": data.get(k), "page": doc["pages"][0]["page"], "model": model}
    return out


def extract_document(doc: dict) -> dict:
    """Returns {textract: {...}, bedrock: {...}} for one document group."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        ft = pool.submit(_textract_for_doc, doc)
        fb = pool.submit(_bedrock_for_doc, doc)
        return {"textract": ft.result(), "bedrock": fb.result()}
