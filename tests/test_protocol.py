"""Control protocol: built only from the audit log, cites evidence, carries the footer."""
from fastapi.testclient import TestClient

from extractor import audit, protocol
from extractor.api import app
from tests.test_audit import _fake_run


def test_protocol_has_header_checks_drafts_decisions_and_footer(audit_dir, raw04):
    rid = _fake_run(raw04, package_id="case-04")
    audit.record_decision(rid, {"check": "XDOC_LAST_NAME", "action": "confirm", "reviewer": "ana",
                                "comment": "typo, same person"})
    ctx = protocol.build(rid)
    html = protocol.render_html(ctx)
    for must in (rid, "case-04", ctx["rules_version"], "ab" * 32, "Recommended next step",
                 "XDOC_LAST_NAME", "RC_VALID", "ana", "typo, same person",
                 "Czech (to send)", "Vážená", protocol.FOOTER, "VERIFIED"):
        assert must in html, must
    # evidence cites both engines with confidence
    assert "T=" in html and "B=" in html
    # deterministic: same run -> same content except the generation timestamp
    again = protocol.render_html(protocol.build(rid))
    strip = lambda h: h.replace(ctx["generated_at"], "")
    assert strip(html).count("<tr>") == strip(again).count("<tr>")


def test_protocol_escapes_values(audit_dir, raw04):
    raw04[0]["raw"]["bedrock"].setdefault("first_name", {})["value"] = "<script>x</script>"
    rid = _fake_run(raw04)
    html = protocol.render_html(protocol.build(rid))
    assert "<script>x" not in html and "&lt;script&gt;" in html


def test_protocol_pdf_and_endpoint(audit_dir, raw04, monkeypatch):
    monkeypatch.delenv("EXTRACTOR_API_KEY", raising=False)
    rid = _fake_run(raw04)
    c = TestClient(app)
    r = c.get(f"/runs/{rid}/protocol")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert protocol.FOOTER in r.text
    r = c.get(f"/runs/{rid}/protocol?format=pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    import pymupdf as fitz
    import unicodedata
    text = unicodedata.normalize("NFKC", "".join(p.get_text() for p in fitz.open(stream=r.content, filetype="pdf")))
    assert "not an automatic final approval" in " ".join(text.split())
    assert c.get("/runs/20260101T000000Z-deadbeef/protocol").status_code == 404
    assert c.get(f"/runs/{rid}/protocol?format=docx").status_code == 422
