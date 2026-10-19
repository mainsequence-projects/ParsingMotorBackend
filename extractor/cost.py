"""Pay-per-call cost meter. Aborts if estimated spend exceeds BUDGET_USD.

No provisioned resources are created. Prices are AWS list-price estimates for
eu-central-1 (Cross-Region Inference eu.* profiles); update if AWS changes them.
Carlos cap for this build: 5 USD. Hackathon account hard max: 800 USD (his OK).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# ---- list-price estimates (USD), documented 2026-10 ----
# Textract AnalyzeDocument with Queries: ~$0.015 / page (Queries tier)
TEXTRACT_QUERIES_PER_PAGE = 0.015
# Bedrock on-demand via eu.* inference profiles (approx input/output per 1M tokens)
BEDROCK_PRICES = {
    # model id substring -> (in_per_MTok, out_per_MTok)
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-2025": (3.00, 15.00),
    "nova-lite": (0.06, 0.24),
    "nova-micro": (0.035, 0.14),
    "default": (3.00, 15.00),  # conservative = Sonnet
}
BUDGET_USD = float(__import__("os").getenv("EXTRACTOR_BUDGET_USD", "5.0"))
LEDGER_PATH = Path(__import__("os").getenv(
    "EXTRACTOR_COST_LEDGER", str(Path(__file__).resolve().parents[1] / "samples" / "out" / "cost_ledger.json")))

_lock = threading.Lock()
_state = {
    "budget_usd": BUDGET_USD,
    "total_usd": 0.0,
    "textract_pages": 0,
    "bedrock_calls": 0,
    "bedrock_input_tokens": 0,
    "bedrock_output_tokens": 0,
    "entries": [],
    "aborted": False,
}


def _price_for(model_id: str) -> tuple[float, float]:
    mid = (model_id or "").lower()
    for key, pair in BEDROCK_PRICES.items():
        if key != "default" and key in mid:
            return pair
    return BEDROCK_PRICES["default"]


class BudgetExceeded(RuntimeError):
    pass


def _persist():
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps({
            **{k: v for k, v in _state.items() if k != "entries"},
            "entries_tail": _state["entries"][-50:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    except Exception:
        pass


def _check(extra: float):
    if _state["total_usd"] + extra > BUDGET_USD:
        _state["aborted"] = True
        _persist()
        raise BudgetExceeded(
            f"Cost meter would reach ${_state['total_usd'] + extra:.4f} "
            f"(budget ${BUDGET_USD:.2f}). STOP — ask Carlos before spending more."
        )


def record_textract_page(pages: int = 1, note: str = ""):
    with _lock:
        cost = TEXTRACT_QUERIES_PER_PAGE * pages
        _check(cost)
        _state["textract_pages"] += pages
        _state["total_usd"] += cost
        _state["entries"].append({"type": "textract", "pages": pages, "usd": round(cost, 6), "note": note})
        _persist()
        return cost


def record_bedrock(model_id: str, input_tokens: int, output_tokens: int, note: str = ""):
    with _lock:
        inn, out = _price_for(model_id)
        cost = (input_tokens / 1_000_000) * inn + (output_tokens / 1_000_000) * out
        _check(cost)
        _state["bedrock_calls"] += 1
        _state["bedrock_input_tokens"] += input_tokens
        _state["bedrock_output_tokens"] += output_tokens
        _state["total_usd"] += cost
        _state["entries"].append({
            "type": "bedrock", "model": model_id,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "usd": round(cost, 6), "note": note,
        })
        _persist()
        return cost


def snapshot() -> dict:
    with _lock:
        return {
            "budget_usd": _state["budget_usd"],
            "total_usd": round(_state["total_usd"], 4),
            "remaining_usd": round(_state["budget_usd"] - _state["total_usd"], 4),
            "textract_pages": _state["textract_pages"],
            "bedrock_calls": _state["bedrock_calls"],
            "bedrock_input_tokens": _state["bedrock_input_tokens"],
            "bedrock_output_tokens": _state["bedrock_output_tokens"],
            "aborted": _state["aborted"],
        }


def reset_for_tests():
    """Only for unit tests — does not delete the ledger file history."""
    with _lock:
        _state.update(total_usd=0.0, textract_pages=0, bedrock_calls=0,
                      bedrock_input_tokens=0, bedrock_output_tokens=0,
                      entries=[], aborted=False)
