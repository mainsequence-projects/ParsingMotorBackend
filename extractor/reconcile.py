"""Deterministic reconciler: Textract vs Bedrock. The LLM never chooses the value."""
from .config import AMOUNT_TOLERANCE, SCHEMAS, TEXTRACT_PASS_CONF
from .normalize import normalize, values_agree
from .rules import parse_birth_number

# Field types where a confident Textract reading beats a disagreeing LLM (OCR-native).
OCR_PREFERRED = {"amount", "date", "birth_number", "iban", "id", "name"}


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

        agree = False
        if t_norm is not None and b_norm is not None:
            if ftype == "amount":
                base = max(abs(t_norm), abs(b_norm), 1.0)
                agree = abs(t_norm - b_norm) / base <= AMOUNT_TOLERANCE
            else:
                agree = values_agree(ftype, t_norm, b_norm)

        if t_norm is None and b_norm is None:
            status = "missing"
            chosen_raw, chosen_norm = None, None
        elif agree:
            status = "passed" if conf >= TEXTRACT_PASS_CONF or t_norm is None else "warning"
            # Prefer Textract raw (keeps original formatting) when present
            chosen_raw = t_raw if t_raw not in (None, "") else b_raw
            chosen_norm = t_norm if t_norm is not None else b_norm
            if t_norm is None:
                status = "warning"  # Bedrock-only agreement path (e.g. doc_list)
                chosen_raw, chosen_norm = b_raw, b_norm
        elif t_norm is None and b_norm is not None:
            status = "warning"  # Bedrock-only field
            chosen_raw, chosen_norm = b_raw, b_norm
        elif b_norm is None and t_norm is not None:
            status = "manual_review" if conf < TEXTRACT_PASS_CONF else "warning"
            chosen_raw, chosen_norm = t_raw, t_norm
        else:
            # Disagreement: LLM must NOT silently win. Prefer OCR for OCR-native types
            # when confidence is decent AND (for amounts) the OCR figure is not an
            # obvious truncation vs Bedrock (e.g. "5250" vs 52000).
            status = "manual_review"
            prefer_ocr = ftype in OCR_PREFERRED and conf >= 0.4 and t_norm is not None
            if prefer_ocr and ftype == "amount" and b_norm is not None:
                hi, lo = max(abs(t_norm), abs(b_norm)), min(abs(t_norm), abs(b_norm)) or 1.0
                if hi / lo >= 5:  # likely OCR dropped a digit/space
                    prefer_ocr = conf >= 0.85
            if prefer_ocr:
                chosen_raw, chosen_norm = t_raw, t_norm
            else:
                chosen_raw, chosen_norm = b_raw, b_norm

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
        info = parse_birth_number(str(bn["normalized"]))
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
