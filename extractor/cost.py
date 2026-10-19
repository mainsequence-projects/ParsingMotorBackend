"""Pay-per-call cost meter with a persistent cumulative ledger and a hard budget.

- No provisioned resources. Prices are list-price estimates (eu-central-1 / eu.* profiles).
- The ledger (EXTRACTOR_COST_LEDGER) holds the running total across processes; every new
  call checks total + estimated cost <= EXTRACTOR_BUDGET_USD or raises BudgetExceeded.
- Cache hits are free and counted separately.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

TEXTRACT_QUERIES_PER_PAGE = 0.015
BEDROCK_PRICES = {  # USD per 1M tokens (input, output)
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-2025": (3.00, 15.00),
    "nova-lite": (0.06, 0.24),
    "nova-micro": (0.035, 0.14),
    "default": (3.00, 15.00),
}


def _budget() -> float:
    return float(os.getenv("EXTRACTOR_BUDGET_USD", "5.0"))


def _ledger_path() -> Path:
    env = os.getenv("EXTRACTOR_COST_LEDGER")
    if env:
        return Path(env)
    cand = Path(__file__).resolve().parents[1] / ".cost" / "ledger.json"
    try:
        cand.parent.mkdir(parents=True, exist_ok=True)
        return cand
    except OSError:
        return Path("/tmp/extractor_cost_ledger.json")


class BudgetExceeded(RuntimeError):
    pass


_lock = threading.Lock()
_COUNTERS = ("total_usd", "textract_pages", "bedrock_calls", "bedrock_input_tokens",
             "bedrock_output_tokens", "cache_hits_textract", "cache_hits_bedrock")


def _empty():
    return {k: 0 for k in _COUNTERS} | {"total_usd": 0.0, "entries_tail": [], "aborted": False}


def _load() -> dict:
    p = _ledger_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            base = _empty()
            base.update({k: data.get(k, base[k]) for k in base})
            return base
        except Exception:
            pass
    return _empty()


def _save(state: dict):
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state["entries_tail"] = state["entries_tail"][-100:]
    out = {**state, "budget_usd": _budget(), "updated_at": datetime.now(timezone.utc).isoformat()}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(p)


def _price_for(model_id: str):
    mid = (model_id or "").lower()
    for key, pair in BEDROCK_PRICES.items():
        if key != "default" and key in mid:
            return pair
    return BEDROCK_PRICES["default"]


def _charge(cost: float, entry: dict, counters: dict):
    with _lock:
        st = _load()
        if st["total_usd"] + cost > _budget():
            st["aborted"] = True
            _save(st)
            raise BudgetExceeded(
                f"cost meter would reach ${st['total_usd'] + cost:.4f} > budget ${_budget():.2f}. "
                "STOP — ask Carlos before spending more.")
        st["total_usd"] += cost
        for k, v in counters.items():
            st[k] += v
        st["entries_tail"].append({**entry, "usd": round(cost, 6),
                                   "ts": datetime.now(timezone.utc).isoformat()})
        _save(st)
        return cost


def estimate_textract(pages: int = 1) -> float:
    return TEXTRACT_QUERIES_PER_PAGE * pages


def precheck(cost: float):
    """Raise before an AWS call if it would exceed the budget."""
    with _lock:
        st = _load()
        if st["total_usd"] + cost > _budget():
            raise BudgetExceeded(f"budget ${_budget():.2f} would be exceeded "
                                 f"(spent ${st['total_usd']:.4f}, next ~${cost:.4f})")


def record_textract_page(pages: int = 1, note: str = ""):
    return _charge(estimate_textract(pages), {"type": "textract", "pages": pages, "note": note},
                   {"textract_pages": pages})


def record_bedrock(model_id: str, input_tokens: int, output_tokens: int, note: str = ""):
    inn, out = _price_for(model_id)
    cost = input_tokens / 1e6 * inn + output_tokens / 1e6 * out
    return _charge(cost, {"type": "bedrock", "model": model_id, "input_tokens": input_tokens,
                          "output_tokens": output_tokens, "note": note},
                   {"bedrock_calls": 1, "bedrock_input_tokens": input_tokens,
                    "bedrock_output_tokens": output_tokens})


def record_cache_hit(kind: str):
    key = "cache_hits_textract" if kind == "textract" else "cache_hits_bedrock"
    with _lock:
        st = _load()
        st[key] += 1
        _save(st)


def snapshot() -> dict:
    with _lock:
        st = _load()
    return {"budget_usd": _budget(), "total_usd": round(st["total_usd"], 4),
            "remaining_usd": round(_budget() - st["total_usd"], 4),
            **{k: st[k] for k in _COUNTERS if k != "total_usd"}, "aborted": st["aborted"],
            "ledger": str(_ledger_path())}


def run_delta(before: dict) -> dict:
    now = snapshot()
    d = {k: (round(now[k] - before[k], 6) if isinstance(now[k], float) else now[k] - before[k])
         for k in ("total_usd",) + tuple(k for k in _COUNTERS if k != "total_usd")}
    d["usd"] = round(d.pop("total_usd"), 4)
    return d
