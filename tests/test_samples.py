"""Synthetic samples must be readable by a human and their truth must be self-consistent.

Why: in v0.2 a long label was printed over its value and the 'engine error' was really a
broken sample. Every generated (non-scanned) page is checked for overlapping text, and every
ground truth must describe 5-7 different document types required by the Contract.
"""
import json
from pathlib import Path

import pymupdf as fitz
import pytest

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PKGS = sorted(p for p in SAMPLES.iterdir() if (p / "ground_truth.json").exists())
PDFS = [f for p in PKGS for f in sorted(p.glob("*.pdf")) if "scan" not in f.name]


@pytest.mark.parametrize("pdf", PDFS, ids=lambda f: f"{f.parent.name}/{f.name}")
def test_no_text_overprints_other_text(pdf):
    for page in fitz.open(pdf):
        spans = [fitz.Rect(s["bbox"]) for b in page.get_text("dict")["blocks"]
                 for ln in b.get("lines", []) for s in ln["spans"] if s["text"].strip()]
        for i, a in enumerate(spans):
            for b in spans[i + 1:]:
                inter = a & b
                assert inter.is_empty or inter.width < 1 or inter.height < 1, f"{pdf.name} p{page.number + 1}: {a} vs {b}"


@pytest.mark.parametrize("pkg", PKGS, ids=lambda p: p.name)
def test_contract_requires_5_to_7_different_types(pkg):
    gt = json.loads((pkg / "ground_truth.json").read_text())
    req = gt["required_documents"]
    assert req[0] == "contract" and len(set(req)) == len(req) and 5 <= len(req) <= 7
    assert sorted(gt["expected_fields"]["contract"]["required_documents"]) == sorted(req[1:])


def test_package_set_covers_the_brief():
    gts = [json.loads((p / "ground_truth.json").read_text()) for p in PKGS]
    steps = {g["expected_next_step"] for g in gts}
    assert {"approve_ready", "request_missing_documents", "manual_review_discrepancy"} <= steps
    assert any(g["expected_missing"] for g in gts)                       # a missing document
    assert any(g["expected_missing_pages"] for g in gts)                 # a missing page
    assert any(len(g["required_documents"]) == 5 for g in gts)           # a 5-type contract
    assert any(len(g["required_documents"]) == 7 for g in gts)           # a 7-type contract
    assert any(any("scan" in i for i in g["inputs"]) for g in gts)       # scanned
    assert any(g["description"].get("shuffle") for g in gts)             # shuffled pages
    for t in ("contract", "id_card", "bank_statement", "payslip", "proof_of_address",
              "employer_confirmation", "tax_or_debt_certificate"):
        assert len({g["layouts"][t] for g in gts if t in g["expected_docs"]}) >= 2, t
