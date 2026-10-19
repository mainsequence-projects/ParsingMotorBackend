"""Deterministic reconciler: Textract vs Bedrock. The LLM never chooses the value."""
from .config import AMOUNT_TOLERANCE, SCHEMAS, TEXTRACT_PASS_CONF
from .normalize import normalize, values_agree
from .rules import parse_birth_number


def reconcile_fields(doc_type: str, textract: dict, bedrock: dict) -> dict:
    fields_meta = SCHEMAS.get(doc_type, {}).get("fields") or {}
    out = {}
    for name, meta in fields_meta.items():
        ftype = meta.get("type", "text")
        t = textract.get(name) or {}
        b = bedrock.get(name) or {}
        t_raw, b_raw = t.get("value"), b.get("value")
        t_norm, b_norm = normalize(ftype, t_raw), normalize(ftype, b_raw)
        conf = float(t.get("confidence") or 0.0)
        page = t.get("page") or b.get("page")

        # Prefer Bedrock value when they agree or Textract missing; keep both for audit.
        chosen_raw = b_raw if b_raw not in (None, "", []) else t_raw
        chosen_norm = b_norm if b_norm is not None else t_norm

        if t_norm is None and b_norm is None:
            status = "missing"
        elif t_norm is not None and b_norm is not None and values_agree(ftype, t_norm, b_norm):
            status = "passed" if conf >= TEXTRACT_PASS_CONF else "warning"
        elif ftype == "amount" and t_norm is not None and b_norm is not None:
            base = max(abs(t_norm), abs(b_norm), 1.0)
            if abs(t_norm - b_norm) / base <= AMOUNT_TOLERANCE:
                status = "passed" if conf >= TEXTRACT_PASS_CONF else "warning"
            else:
                status = "manual_review"
        elif t_norm is None or b_norm is None:
            # One engine has it -> still usable but needs a human glance
            status = "warning" if (t_norm or b_norm) else "missing"
            if t_norm is None and b_norm is not None:
                status = "warning"  # Bedrock-only (e.g. doc_list)
            elif b_norm is None and t_norm is not None:
                status = "manual_review"
        else:
            status = "manual_review"

        out[name] = {
            "value": chosen_raw,
            "normalized": chosen_norm,
            "textract": t_raw,
            "bedrock": b_raw,
            "confidence": round(conf, 3),
            "page": page,
            "status": status,
            "bbox": t.get("bbox"),
        }
    return out


def explain_disagreement(doc_type: str, field: str, textract_v, bedrock_v) -> str | None:
    """Optional one-sentence LLM explanation. Never chooses the value."""
    from .aws import converse_json
    try:
        data, _ = converse_json(
            f"In one short English sentence, why might Textract read '{textract_v}' "
            f"and a vision model read '{bedrock_v}' for field '{field}' on a {doc_type}? "
            'Return JSON {"explanation":"..."}. Do not pick which value is correct.',
            max_tokens=120,
        )
        return data.get("explanation")
    except Exception:
        return None


def run_field_rules(fields: dict) -> list[dict]:
    """Per-document deterministic rules (birth number, date consistency)."""
    checks = []
    bn = fields.get("birth_number") or {}
    if bn.get("normalized"):
        info = parse_birth_number(bn["normalized"])
        checks.append({
            "check": "birth_number_valid",
            "status": "passed" if info["valid"] else "discrepancy",
            "detail": info["reason"],
            "page": bn.get("page"),
        })
        if info.get("valid") and info.get("birth_date"):
            bd = fields.get("birth_date") or {}
            if bd.get("normalized") and bd["normalized"] != info["birth_date"]:
                checks.append({
                    "check": "birth_date_matches_birth_number",
                    "status": "discrepancy",
                    "detail": f"field={bd['normalized']} encoded={info['birth_date']}",
                    "page": bd.get("page") or bn.get("page"),
                })
            elif bd.get("normalized"):
                checks.append({
                    "check": "birth_date_matches_birth_number",
                    "status": "passed",
                    "detail": "ok",
                    "page": bd.get("page") or bn.get("page"),
                })
    return checks
