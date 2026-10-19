"""Synthetic samples must be readable by a human: no label printed over its value."""
from pathlib import Path

import pymupdf as fitz
import pytest

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.mark.parametrize("pkg", ["01_clean", "02_missing_payslip", "03_amount_mismatch",
                                 "04_name_typo_invalid_rc", "09_photo_id"])
def test_contract_labels_do_not_overprint_values(pkg):
    page = fitz.open(SAMPLES / pkg / "contract.pdf")[0]
    spans = [s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]
             if s["text"].strip()]
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            ra, rb = fitz.Rect(a["bbox"]), fitz.Rect(b["bbox"])
            inter = ra & rb
            assert inter.is_empty or inter.width < 1, f"{a['text']!r} overlaps {b['text']!r}"
