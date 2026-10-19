"""HTTP contract for Jose: status codes and error bodies (no AWS: failures happen before)."""
import base64
import io

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from extractor import pipeline
from extractor.api import app

C = TestClient(app)


def _pdf(pages=1, **save):
    d = fitz.open()
    for _ in range(pages):
        d.new_page().insert_text((72, 72), "synthetic")
    b = io.BytesIO()
    d.save(b, **save)
    return b.getvalue()


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("AWS pipeline must not be reached for invalid input")
    monkeypatch.setattr(pipeline, "run_package", boom)


@pytest.mark.parametrize("name,data,code", [
    ("empty.pdf", b"", 400),
    ("corrupt.pdf", b"%PDF-1.4\n garbage", 422),
    ("fake.pdf", b"PK\x03\x04zip", 422),
    ("notes.txt", b"hello", 415),
    ("bad.png", b"\x89PNG\r\n\x1a\n broken", 422),
    ("enc.pdf", _pdf(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o"), 422),
    ("long.pdf", _pdf(pages=30), 413),
])
def test_bad_uploads_are_4xx_not_500(name, data, code):
    r = C.post("/extract", files={"files": (name, data)})
    assert r.status_code == code, r.text
    assert isinstance(r.json()["detail"], str)


def test_owner_only_encrypted_pdf_is_accepted(monkeypatch):
    seen = {}
    monkeypatch.setattr(pipeline, "run_package", lambda pages, **k: seen.setdefault("n", len(pages)) and {"ok": 1})
    data = _pdf(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="")
    r = C.post("/extract", files={"files": ("owner.pdf", data)})
    assert r.status_code == 200 and seen["n"] == 1


def test_image_without_extension_is_sniffed(monkeypatch):
    monkeypatch.setattr(pipeline, "run_package", lambda pages, **k: {"pages": len(pages)})
    b = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(b, "PNG")
    r = C.post("/extract", files={"files": ("blob", b.getvalue())})
    assert r.status_code == 200 and r.json()["pages"] == 1


@pytest.mark.parametrize("body,code", [
    ([1, 2], 400), ({"files": ["x"]}, 400), ({"files": [{"filename": "a.pdf"}]}, 400),
    ({"files": [{"filename": "a.pdf", "content_base64": "@@@"}]}, 400), ({}, 400),
])
def test_extract_json_bad_bodies(body, code):
    r = C.post("/extract_json", json=body)
    assert r.status_code == code, r.text


def test_extract_json_not_json():
    r = C.post("/extract_json", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_extract_json_bad_pdf_is_422():
    r = C.post("/extract_json", json={"files": [{"filename": "a.pdf",
                                                  "content_base64": base64.b64encode(b"%PDF-x").decode()}]})
    assert r.status_code == 422


def test_audit_endpoints_status_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_AUDIT_DIR", str(tmp_path))
    (tmp_path / "index.jsonl").write_text('{"ts":"x","run_id":"y"}\n')
    dec = {"field": "contract.first_name", "action": "confirm", "reviewer": "x"}
    for rid in ("20990101T000000Z-deadbeef", "index", "nope"):
        assert C.get(f"/runs/{rid}").status_code == 404
        assert C.get(f"/runs/{rid}/verify").status_code == 404
        assert C.post(f"/runs/{rid}/decisions", json=dec).status_code == 404
    assert C.post("/runs/x/decisions", json={**dec, "action": "approve"}).status_code == 422
    assert C.get("/learning?min_support=-5").status_code == 422


def test_long_extraction_does_not_block_health(monkeypatch):
    import threading
    import time
    started = threading.Event()

    def slow(pages, **k):
        started.set()
        time.sleep(1.5)
        return {"ok": True}
    monkeypatch.setattr(pipeline, "run_package", slow)
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, "PNG")
    with TestClient(app) as c:      # one shared event loop, like uvicorn
        t = threading.Thread(target=lambda: c.post("/extract", files={"files": ("a.png", b.getvalue())}))
        t.start()
        assert started.wait(5)
        t0 = time.monotonic()
        assert c.get("/health").status_code == 200
        assert time.monotonic() - t0 < 0.8
        t.join()
