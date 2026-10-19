"""FastAPI entrypoint for the KB document-control extractor (Main Sequence discovery).

Discovered as api/kb_extractor/main.py — do not rename this path.
The engine lives in the top-level `extractor` package; this module only re-exports `app`.
"""

from extractor.api import app  # noqa: F401  — Main Sequence / uvicorn target
