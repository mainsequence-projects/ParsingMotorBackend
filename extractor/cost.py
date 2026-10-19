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
import tempfile as _tempfile
_default_ledger = Path(__file__).resolve().parents[1] / "samples" / "out" / "cost_ledger.json"
try:
    _default_ledger.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    _default_ledger = Path("/tmp/extractor_cost_ledger.json")
def _ledger_path():
    env = __import__("os").getenv("EXTRACTOR_COST_LEDGER")
    if env:
        return Path(env)
    cand = Path(__file__).resolve().parents[1] / "samples" / "out" / "cost_ledger.json"
    try:
        cand.parent.mkdir(parents=True, exist_ok=True)
        return cand
    except OSError:
        return Path("/tmp/extractor_cost_ledger.json")
LEDGER_PATH = _ledger_path()
