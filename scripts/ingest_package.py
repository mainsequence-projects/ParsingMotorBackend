"""Ingest one loan package into Back Office: classify, split into documents, upload each one.

Back Office processes one logical document per upload and stops a multi-document file with
`multiple_documents_require_grouping`. Our packages are one PDF with 5-7 documents (shuffled,
missing pages), so this splits them first with the parser's own observations.

  # 1. split (calls the parser: --parser deployed = the Lambda, local = this checkout)
  python scripts/ingest_package.py split /path/13_happy_all7_D --out /tmp/13 --parser deployed
  # 2. upload to Back Office (local stack or a trusted API), into a new loan case
  python scripts/ingest_package.py upload /tmp/13 --api http://127.0.0.1:15174/api
  # or both:
  python scripts/ingest_package.py run /path/13_happy_all7_D --out /tmp/13 --api http://127.0.0.1:15174/api

Splitting is a reading observation (pages, types, printed page numbers); Back Office still
classifies, extracts, reviews and evaluates every uploaded document itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extractor.splitter import group_v2_pages  # noqa: E402
from scripts.eval_v2 import CATALOG, files_for  # noqa: E402

PUBLIC_URL = "https://d31su4idgeitei.cloudfront.net/"
INPUT_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png"}
NAMESPACE = uuid.UUID("6f1d7c2a-6b0e-4d61-9f0e-1c6b1a2e9d10")   # stable idempotency keys
TERMINAL = {"completed", "needs_input", "failed"}


# ---------------- split ----------------
def package_inputs(pkg: Path) -> list[str]:
    gt = pkg / "ground_truth.json"
    if gt.exists():
        return json.loads(gt.read_text())["inputs"]
    return sorted(p.name for p in pkg.iterdir() if p.suffix.lower() in INPUT_SUFFIXES and p.name.startswith("package"))


def classifier(mode: str, catalog: list[dict]):
    if mode == "deployed":
        from deploy.signed_extract import signed_post
        return lambda files: signed_post("classify_json", {"files": files, "document_types": catalog,
                                                           "contract_version": 2}, PUBLIC_URL)
    from fastapi.testclient import TestClient

    from extractor import api
    api.API_KEY = ""
    client = TestClient(api.app)

    def local(files):
        r = client.post("/classify_json", json={"files": files, "document_types": catalog, "contract_version": 2})
        r.raise_for_status()
        return r.json()
    return local


def write_document(src: Path, pages: list[int], dest: Path) -> None:
    """One PDF per logical document, pages in reading order (images are copied as they are)."""
    if src.suffix.lower() != ".pdf":
        dest.with_suffix(src.suffix.lower()).write_bytes(src.read_bytes())
        return
    import pymupdf
    with pymupdf.open(src) as doc, pymupdf.open() as out:
        for n in pages:
            out.insert_pdf(doc, from_page=n - 1, to_page=n - 1)
        out.save(dest)


def split(pkg: Path, out: Path, classify) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for name in package_inputs(pkg):
        pages += classify(files_for(pkg, [name]))["pages"]
    docs = []
    for i, g in enumerate(group_v2_pages(pages), 1):
        kind = g["doc_type"] or "unclassified"
        numbers = [p["page"] for p in g["pages"]]
        src = pkg / g["source_file"]
        target = out / f"{i:02d}_{kind}_p{'-'.join(map(str, numbers))}.pdf"
        write_document(src, numbers, target)
        target = target if target.exists() else target.with_suffix(src.suffix.lower())
        docs.append({"file": target.name, "doc_type": g["doc_type"], "source_file": g["source_file"],
                     "pages": numbers, "confidence": g["confidence"],
                     "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    manifest = {"package": pkg.name, "pages": len(pages), "documents": docs,
                "unclassified_pages": sum(len(d["pages"]) for d in docs if d["doc_type"] is None)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


# ---------------- upload ----------------
class BackOffice:
    """Minimal client for the documented Back Office REST API (JSON + one multipart route)."""

    def __init__(self, base: str, http=None):
        self.base = base.rstrip("/")
        self.http = http or self._urlopen

    @staticmethod
    def _urlopen(method, url, body, headers):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")

    def call(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        return self.http(method, self.base + path, body, {"Content-Type": "application/json"})

    def upload(self, person_uid: str, op: str, path: Path):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("person_uid", person_uid), ("operation_key", op)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        media = "application/pdf" if path.suffix == ".pdf" else "image/jpeg" if path.suffix in (".jpg", ".jpeg") else "image/png"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                     f"Content-Type: {media}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        return self.http("POST", self.base + "/document-processing", body,
                         {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def op_key(*parts) -> str:
    """Same package + same step -> same key, so a rerun after a lost response never duplicates."""
    return str(uuid.uuid5(NAMESPACE, ":".join(map(str, parts))))


def upload(folder: Path, bo: BackOffice, *, person_uid=None, case_uid=None, person_name=None,
           case_type="new_loan", document_set=("loan_requirements", 1), wait_s=600, poll_s=3) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text())
    pkg = manifest["package"]
    if not person_uid:
        person_uid = bo.call("POST", "/persons", {"operation_key": op_key(pkg, "person"), "profile": {
            "external_reference": f"synthetic:{pkg}", "full_name": person_name or pkg}})["uid"]
    if not case_uid:
        case_uid = bo.call("POST", "/cases", {"operation_key": op_key(pkg, "case"), "subject_uid": person_uid,
                                              "case_type_key": case_type, "document_set_key": document_set[0],
                                              "document_set_version": document_set[1]})["uid"]
    runs = {}
    for d in manifest["documents"]:
        runs[d["file"]] = bo.upload(person_uid, op_key(pkg, "upload", d["sha256"]), folder / d["file"])
    deadline = time.monotonic() + wait_s
    while any(r.get("status") not in TERMINAL for r in runs.values()) and time.monotonic() < deadline:
        time.sleep(poll_s)
        for f, r in runs.items():
            if r.get("status") not in TERMINAL:
                runs[f] = bo.call("GET", f"/processing-runs/{r['uid']}")
    documents = [r["document"]["uid"] for r in runs.values() if (r.get("document") or {}).get("uid")]
    linked = None
    if documents:
        case = bo.call("GET", f"/cases/{case_uid}")
        current = case.get("document_uids") or []
        linked = bo.call("PUT", f"/cases/{case_uid}/documents", {
            "operation_key": op_key(pkg, "link", *sorted(documents)), "expected_revision": case["revision"],
            "document_uids": current + [x for x in documents if x not in current]})
    result = {"package": pkg, "person_uid": person_uid, "case_uid": case_uid,
              "runs": {f: {k: r.get(k) for k in ("uid", "status", "stage", "error_code", "document_type")}
                       for f, r in runs.items()},
              "linked_documents": len(documents), "case_revision": (linked or {}).get("revision")}
    (folder / "upload_result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def check_api(url: str, allow_remote: bool) -> None:
    u = urlparse(url)
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"):
        return
    if not (allow_remote and u.scheme == "https"):
        raise SystemExit("refusing a non-loopback Back Office API; pass an https URL with --allow-remote")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("split", "run"):
        s = sub.add_parser(name)
        s.add_argument("package", type=Path)
        s.add_argument("--out", type=Path, required=True)
        s.add_argument("--parser", choices=["deployed", "local"], default="deployed")
        s.add_argument("--catalog", type=Path, help="JSON {document_type: description} for the classifier")
    up = [sub.add_parser("upload"), sub.choices["run"]]
    up[0].add_argument("folder", type=Path)
    for s in up:
        s.add_argument("--api", required=True, help="Back Office API base, e.g. http://127.0.0.1:15174/api")
        s.add_argument("--allow-remote", action="store_true")
        s.add_argument("--person-uid")
        s.add_argument("--case-uid")
        s.add_argument("--person-name")
        s.add_argument("--document-set", default="loan_requirements:1")
    a = ap.parse_args(argv)
    if a.cmd in ("split", "run"):
        catalog = [dict(c) for c in CATALOG]
        if a.catalog:
            override = json.loads(a.catalog.read_text(encoding="utf-8"))
            for c in catalog:
                c["description"] = override.get(c["document_type"], c["description"])
        m = split(a.package, a.out, classifier(a.parser, catalog))
        print(json.dumps({"split": [(d["file"], d["confidence"]) for d in m["documents"]],
                          "unclassified_pages": m["unclassified_pages"]}, ensure_ascii=False))
    if a.cmd in ("upload", "run"):
        check_api(a.api, a.allow_remote)
        key, _, ver = a.document_set.partition(":")
        r = upload(a.folder if a.cmd == "upload" else a.out, BackOffice(a.api), person_uid=a.person_uid,
                   case_uid=a.case_uid, person_name=a.person_name, document_set=(key, int(ver or 1)))
        print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
