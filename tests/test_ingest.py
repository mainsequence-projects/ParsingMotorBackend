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


def _png(img):
    import io
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def test_poster_size_pdf_page_is_rendered_bounded():
    import io
    import time

    import pymupdf as fitz
    from extractor.ingest import MAX_SIDE, load_bytes
    d = fitz.open()
    d.new_page(width=14400, height=14400)      # 5 m x 5 m page: 30000 px at 150 dpi
    b = io.BytesIO()
    d.save(b)
    t0 = time.monotonic()
    page = load_bytes("poster.pdf", b.getvalue())[0]
    from PIL import Image
    assert max(Image.open(io.BytesIO(page.png)).size) <= MAX_SIDE
    assert time.monotonic() - t0 < 5


def test_a4_render_unchanged_so_hashes_stay_stable():
    import pymupdf as fitz
    from PIL import Image

    from extractor.config import RENDER_DPI
    from extractor.ingest import load_bytes, to_png
    data = (SAMPLES / "01_clean" / "contract.pdf").read_bytes()
    pix = fitz.open(stream=data, filetype="pdf")[0].get_pixmap(dpi=RENDER_DPI)   # v0.2 render
    old = to_png(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    assert load_bytes("c.pdf", data)[0].png == old


def test_huge_image_is_413_before_decoding():
    import pytest
    from PIL import Image

    from extractor.ingest import InputError, load_bytes
    with pytest.raises(InputError) as e:
        load_bytes("bomb.png", _png(Image.new("1", (12000, 12000))))
    assert e.value.status == 413


def test_exif_orientation_is_applied():
    import io

    from PIL import Image

    from extractor.ingest import load_bytes
    img = Image.new("RGB", (300, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6                                # rotate 90° CW on display
    b = io.BytesIO()
    img.save(b, "JPEG", exif=exif.tobytes())
    out = Image.open(io.BytesIO(load_bytes("phone.jpg", b.getvalue())[0].png))
    assert out.size == (100, 300)


def test_big_png_goes_to_bedrock_as_jpeg():
    import os

    from PIL import Image

    from extractor.aws import BEDROCK_MAX_IMAGE_BYTES, _image_block
    noisy = Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3))
    png = _png(noisy)
    assert len(png) > BEDROCK_MAX_IMAGE_BYTES
    blk = _image_block(png)["image"]
    assert blk["format"] == "jpeg" and len(blk["source"]["bytes"]) < 3_750_000
    small = _png(Image.new("RGB", (100, 100)))
    assert _image_block(small)["image"]["format"] == "png"
