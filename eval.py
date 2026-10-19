"""Moved to scripts/eval.py (9 packages, stricter metrics). Kept as a shim."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).parent / "scripts" / "eval.py"), run_name="__main__")
