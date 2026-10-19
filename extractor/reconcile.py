"""Deterministic reconciler: Textract vs Bedrock, parameterised by rules.yaml (FIELD_RECONCILE)."""
from __future__ import annotations

from .config import SCHEMAS
from .doclist import parse_required_documents
from .normalize import normalize, values_agree
from .rules_engine import Rules

RULE_ID = "FIELD_RECONCILE"


def _amount_agree(a, b, tol):
    base = max(abs(a), abs(b), 1.0)
    return abs(a - b) / base <= tol


def reconcile_fields(doc_type: str, textract: dict, bedrock: dict, rules: Rules) -> dict:
    p = rules.params(RULE_ID)
    fields_meta = SCHEMAS.get(doc_type, {}).get("fields") or {}
    out = {}
    for name, meta in fields_meta.items():
        ftype = meta.get("type", "text")
        if meta.get("textract") == "lines":   # deterministic list parser over raw OCR lines
            t = parse_required_documents(textract.get("_lines"),
                                         rules.params("COMPLETENESS").get("list_parser")) or {}
        else:
            t = textract.get(name) or {}
        b = bedrock.get(name) or {}
        t_raw, b_raw = t.get("value"), b.get("value")
        t_norm, b_norm = normalize(ftype, t_raw), normalize(ftype, b_raw)
        conf = float(t.get("confidence") or 0.0)

        agree = False
        if t_norm is not None and b_norm is not None:
            if ftype == "amount":
                agree = _amount_agree(t_norm, b_norm, p["amount_tolerance"])
            elif ftype in ("address", "text"):
                agree = values_agree(ftype, t_norm, b_norm, p["text_similarity"])
            else:
                agree = t_norm == b_norm

        if t_norm is None and b_norm is None:
            outcome, chosen_raw, chosen_norm, source = "both_missing", None, None, None
        elif agree:
            outcome = "agree_high_conf" if conf >= p["pass_conf"] else "agree_low_conf"
            chosen_raw, chosen_norm = (t_raw, t_norm) if t_raw not in (None, "") else (b_raw, b_norm)
            source = "both"
        elif t_norm is None and t_raw not in (None, "") and b_norm is not None:
            # OCR printed something the strict normaliser cannot parse ('15200E.00'):
            # that is a disagreement, not an LLM-only field.
            outcome = "ocr_unparseable" if "ocr_unparseable" in rules.rule(RULE_ID)["severity"] else "disagree"
            chosen_raw, chosen_norm, source = b_raw, b_norm, "bedrock"
        elif t_norm is None:
            outcome, chosen_raw, chosen_norm, source = "llm_only", b_raw, b_norm, "bedrock"
        elif b_norm is None:
            outcome = "ocr_only_high_conf" if conf >= p["pass_conf"] else "ocr_only_low_conf"
            chosen_raw, chosen_norm, source = t_raw, t_norm, "textract"
        else:
            outcome = "disagree"
            if ftype == "amount":
                prefer_ocr = conf >= p["amount_ocr_wins_min_conf"]
            else:
                prefer_ocr = ftype in p["ocr_preferred_types"] and conf >= p["ocr_preferred_min_conf"]
            chosen_raw, chosen_norm = (t_raw, t_norm) if prefer_ocr else (b_raw, b_norm)
            source = "textract" if prefer_ocr else "bedrock"

        out[name] = {
            "type": ftype,
            "value": chosen_raw,
            "normalized": chosen_norm,
            "textract": t_raw,
            "textract_conf": round(conf, 3),
            "textract_page": t.get("page"),
            "bedrock": b_raw,
            "bedrock_page": b.get("page"),
            # confidence/page/bbox cite the engine whose value was kept (null conf = LLM-only value)
            "source": source,
            "confidence": round(conf, 3) if source in ("textract", "both") else None,
            "page": (t.get("page") or b.get("page")) if source == "both"
                    else t.get("page") if source == "textract" else b.get("page") if source == "bedrock" else None,
            "outcome": outcome,
            "status": rules.severity(RULE_ID, outcome),
            "rule_ids": [RULE_ID],
            "rules_version": rules.version,
            "bbox": t.get("bbox") if source in ("textract", "both") else None,
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
