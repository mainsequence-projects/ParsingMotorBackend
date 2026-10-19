"""Package ingest: split a multi-document package with the parser's observations, then upload
each document to Back Office through its documented REST routes (no AWS, fake Back Office)."""

import json
from pathlib import Path

import pymupdf

from scripts import ingest_package as ing
from scripts.eval_v2 import bo_type

PKG = Path(__file__).resolve().parents[1] / "samples" / "09_shuffled_6types"


def oracle_classifier(gt):
    """Pages in physical order with the type and printed numbering the ground truth implies."""
    info = {}
    for d in gt["expected_documents"]:
        for i, n in enumerate(d["pages"]):
            info[(d["source_file"], n)] = (bo_type(d["doc_type"]), i + 1, len(d["pages"]))

    def classify(files):
        name = files[0]["filename"]
        n_pages = max(n for (f, n) in info if f == name)
        pages = []
        for n in range(1, n_pages + 1):
            t, num, count = info[(name, n)]
            pages.append({"source_file": name, "page": n, "document_type": t, "confidence": 0.9,
                          "printed_page_number": num if count > 1 else None, "group_hint": None})
        return {"pages": pages}
    return classify


def test_split_rebuilds_each_document_in_reading_order(tmp_path):
    gt = json.loads((PKG / "ground_truth.json").read_text())
    manifest = ing.split(PKG, tmp_path, oracle_classifier(gt))
    got = sorted((d["doc_type"], tuple(d["pages"])) for d in manifest["documents"])
    exp = sorted((bo_type(d["doc_type"]), tuple(d["pages"])) for d in gt["expected_documents"])
    assert got == exp and manifest["unclassified_pages"] == 0
    for d in manifest["documents"]:
        with pymupdf.open(tmp_path / d["file"]) as doc:
            assert doc.page_count == len(d["pages"])
    assert json.loads((tmp_path / "manifest.json").read_text())["package"] == PKG.name


class FakeBackOffice:
    def __init__(self):
        self.calls, self.n = [], 0

    def __call__(self, method, url, body, headers):
        path = url.split("/api", 1)[1]
        self.calls.append((method, path, body, headers))
        if path == "/persons":
            return {"uid": "person-1"}
        if path == "/cases" and method == "POST":
            return {"uid": "case-1"}
        if path == "/document-processing":
            self.n += 1
            return {"uid": f"run-{self.n}", "status": "queued"}
        if path.startswith("/processing-runs/"):
            run = path.rsplit("/", 1)[1]
            return {"uid": run, "status": "completed", "document": {"uid": "doc-" + run}}
        if path == "/cases/case-1" and method == "GET":
            return {"uid": "case-1", "revision": 1, "document_uids": []}
        if path == "/cases/case-1/documents":
            return {"uid": "case-1", "revision": 2}
        raise AssertionError(path)


def test_upload_creates_case_uploads_every_document_and_links_them(tmp_path):
    gt = json.loads((PKG / "ground_truth.json").read_text())
    manifest = ing.split(PKG, tmp_path, oracle_classifier(gt))
    fake = FakeBackOffice()
    result = ing.upload(tmp_path, ing.BackOffice("http://127.0.0.1:15174/api", http=fake), poll_s=0)
    uploads = [c for c in fake.calls if c[1] == "/document-processing"]
    assert len(uploads) == len(manifest["documents"])
    assert b'name="person_uid"\r\n\r\nperson-1' in uploads[0][2] and b"%PDF" in uploads[0][2]
    case = json.loads(next(c[2] for c in fake.calls if c[1] == "/cases" and c[0] == "POST"))
    assert case["case_type_key"] == "new_loan" and case["subject_uid"] == "person-1"
    link = json.loads(next(c[2] for c in fake.calls if c[1] == "/cases/case-1/documents"))
    assert link["expected_revision"] == 1 and len(link["document_uids"]) == len(manifest["documents"])
    assert result["linked_documents"] == len(manifest["documents"]) and result["case_revision"] == 2

    again = FakeBackOffice()   # a rerun reuses every idempotency key, so Back Office dedupes it
    ing.upload(tmp_path, ing.BackOffice("http://127.0.0.1:15174/api", http=again), poll_s=0)
    keys = lambda calls: [json.loads(c[2])["operation_key"] for c in calls if c[1] in ("/persons", "/cases")]
    assert keys(fake.calls) == keys(again.calls)


def test_upload_refuses_remote_http_api():
    import pytest
    with pytest.raises(SystemExit):
        ing.check_api("http://backoffice.example.com/api", allow_remote=True)
    ing.check_api("http://127.0.0.1:15174/api", allow_remote=False)
