"""Two runs at once: cache and cost ledger must not collide (no AWS)."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from extractor import cache

ROOT = Path(__file__).resolve().parents[1]


def test_same_cache_key_written_concurrently(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_CACHE", "1")
    monkeypatch.setenv("EXTRACTOR_CACHE_DIR", str(tmp_path))
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda i: cache.put("k", {"i": i, "pad": "x" * 20000}), range(64)))
    assert cache.get("k")["pad"] == "x" * 20000
    assert not list(tmp_path.glob("*.tmp"))


def test_ledger_is_exact_across_processes(tmp_path):
    ledger = tmp_path / "ledger.json"
    code = ("import os;from extractor import cost\n"
            "for _ in range(40): cost.record_textract_page(1)\n")
    env = {"EXTRACTOR_COST_LEDGER": str(ledger), "EXTRACTOR_BUDGET_USD": "100", "PATH": "/usr/bin:/bin"}
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env) for _ in range(4)]
    assert all(p.wait(60) == 0 for p in procs)
    d = json.loads(ledger.read_text())
    assert d["textract_pages"] == 160
    assert abs(d["total_usd"] - 160 * 0.015) < 1e-9
