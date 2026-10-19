"""Ingest: names, limits and robustness (no AWS)."""
from pathlib import Path

from extractor.ingest import load_inputs, safe_name

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def test_duplicate_upload_names_stay_separate_sources():
    seen = set()
    assert safe_name("scan.pdf", seen) == "scan.pdf"
    assert safe_name("scan.pdf", seen) == "scan__2.pdf"
    assert safe_name("../../etc/scan.pdf", seen) == "scan__3.pdf"


def test_filename_is_sanitised():
    assert safe_name("..\\..\\evil\x00.pdf") == "evil.pdf"
    assert safe_name("") == "upload.bin"
    assert len(safe_name("a" * 500 + ".pdf")) <= 110


def test_two_files_with_same_name_from_folders_are_not_merged():
    a = SAMPLES / "01_clean" / "contract.pdf"
    b = SAMPLES / "04_name_typo_invalid_rc" / "contract.pdf"
    pages, inputs = load_inputs([a, b])
    assert [p.source_file for p in pages] == ["contract.pdf", "contract__2.pdf"]
    assert inputs[1]["original_name"] == "contract.pdf"
