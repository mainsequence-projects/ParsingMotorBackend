"""HTTP contract: what docs/API.md and docs/openapi.json promise to Jose is what the API returns.

Why: response_model makes FastAPI validate every response; these tests drive each endpoint through
its success path with real engine outputs so a drift in the engine breaks CI, not Jose's frontend.
The demo story (03: discrepancy -> client draft -> human edit -> approve_ready -> verify ok) runs
offline from a recorded audit log, no AWS.
"""
import importlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from extractor import __version__
from extractor import api_models as M

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
RUN = "20261019T125642Z-161958ed"


@pytest.mark.parametrize("path", sorted((ROOT / "samples" / "out").glob("0*.json")), ids=lambda p: p.stem)
def test_every_sample_output_matches_extract_response(path):
    M.ExtractResponse.model_validate(json.loads(path.read_text()))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_AUDIT_DIR", str(tmp_path))
    shutil.copy(FIX / "audit_03_amount_mismatch.jsonl", tmp_path / f"{RUN}.jsonl")
    from extractor.api import app
    return TestClient(app)


def test_demo_story_discrepancy_draft_human_edit_verify(client):
    s = client.get(f"/runs/{RUN}")
    assert s.status_code == 200, s.text
    st = s.json()
    assert st["recommended_next_step"] == "manual_review_discrepancy"
    assert "income figures" in st["draft_client_message"]
    r = client.post(f"/runs/{RUN}/decisions", json={
        "field": "contract.declared_monthly_income", "action": "edit", "new_value": "38000",
        "reviewer": "jose", "comment": "client confirmed 38 000 CZK net; contract had a typo"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["next_step_before"] == "manual_review_discrepancy"
    assert d["next_step"] == "approve_ready"
    assert d["decision_event"]["type"] == "decision"
    after = client.get(f"/runs/{RUN}").json()
    assert after["draft_client_message"] is None and len(after["decisions"]) == 1
    v = client.get(f"/runs/{RUN}/verify").json()
    assert v["ok"] and v["errors"] == [] and v["events"] >= 3
    assert v["file"] == f"{RUN}.jsonl"                    # no server path
    lr = client.get("/learning?min_support=1")
    assert lr.status_code == 200 and lr.json()["auto_applied"] is False


def test_tampered_log_fails_verify(client, tmp_path):
    p = tmp_path / f"{RUN}.jsonl"
    p.write_text(p.read_text().replace("52000", "38000", 1))
    assert client.get(f"/runs/{RUN}/verify").json()["ok"] is False


def test_health_reports_version_rules_and_limits(client):
    h = client.get("/health").json()
    assert h["version"] == __version__ == "0.5.0"
    assert len(h["rules_version"]) == 16 and h["limits"]["max_pages"] >= 1


def test_audit_file_is_not_an_absolute_server_path():
    out = json.loads((FIX / "extract_response_01.json").read_text())
    from extractor import pipeline
    assert "audit_file" in out
    src = Path(pipeline.__file__).read_text()
    assert "str(audit.run_path(run_id))" not in src


def test_openapi_documents_every_endpoint_and_matches_docs_file(client):
    spec = client.get("/openapi.json").json()
    for path, method in [("/health", "get"), ("/session", "get"), ("/extract", "post"),
                         ("/extract_json", "post"), ("/runs/{run_id}", "get"),
                         ("/runs/{run_id}/decisions", "post"), ("/runs/{run_id}/verify", "get"),
                         ("/learning", "get"), ("/rules", "get"), ("/rules/proposals", "get"),
                         ("/rules/proposals/{proposal_id}/review", "post"), ("/runs", "get"),
                         ("/runs/{run_id}/events", "get"), ("/eval/summary", "get")]:
        op = spec["paths"][path][method]
        assert "$ref" in json.dumps(op["responses"]["200"]), path
        if path != "/health":
            assert "401" in op["responses"], path
    assert "401" in spec["paths"]["/runs/{run_id}/pages/{n}.png"]["get"]["responses"]
    assert "requestBody" in spec["paths"]["/extract_json"]["post"]
    on_disk = json.loads((ROOT / "docs" / "openapi.json").read_text())
    assert on_disk == spec, "docs/openapi.json is stale: run python scripts/export_openapi.py"


def _reload_api(monkeypatch, origins):
    import extractor.api as api
    if origins is None:
        monkeypatch.delenv("EXTRACTOR_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("EXTRACTOR_CORS_ORIGINS", origins)
    return importlib.reload(api)


def test_cors_off_by_default_on_with_env(monkeypatch):
    try:
        api = _reload_api(monkeypatch, None)
        r = TestClient(api.app).get("/health", headers={"Origin": "http://localhost:5173"})
        assert "access-control-allow-origin" not in r.headers      # Lambda URL adds its own
        api = _reload_api(monkeypatch, "http://localhost:5173, https://ms.example")
        c = TestClient(api.app)
        pre = c.options("/extract_json", headers={"Origin": "http://localhost:5173",
                                                  "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "content-type,x-api-key"})
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == "http://localhost:5173"
        bad = c.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in bad.headers
    finally:
        _reload_api(monkeypatch, None)


def test_cost_ledger_path_is_not_exposed():
    from extractor.pipeline import _public_ledger
    assert _public_ledger({"total_usd": 1, "ledger": "/tmp/x/.cost/ledger.json"}) == {
        "total_usd": 1, "ledger": "ledger.json"}


def test_upstream_error_text_is_not_returned(monkeypatch):
    import io
    from PIL import Image
    from extractor import pipeline
    from extractor.api import app

    def boom(pages, **k):
        raise RuntimeError("AccessDeniedException: arn:aws:iam::123456789012:role/x is not authorized")
    monkeypatch.setattr(pipeline, "run_package", boom)
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, "PNG")
    r = TestClient(app).post("/extract", files={"files": ("a.png", b.getvalue())})
    assert r.status_code == 502
    assert "123456789012" not in r.text and "RuntimeError" in r.json()["detail"]


def test_every_endpoint_except_health_requires_the_key(monkeypatch, client):
    import extractor.api as api
    monkeypatch.setattr(api, "API_KEY", "k")
    calls = [("get", "/session"), ("post", "/extract"), ("post", "/extract_json"),
             ("get", f"/runs/{RUN}"), ("post", f"/runs/{RUN}/decisions"),
             ("get", f"/runs/{RUN}/verify"), ("get", "/learning"), ("get", "/rules"),
             ("get", "/rules/proposals"), ("post", "/rules/proposals/abc/review")]
    for m, path in calls:
        kw = {"json": {"action": "confirm", "reviewer": "x", "field": "a.b"}} if "decisions" in path else {}
        if path.endswith("/review"):
            kw = {"json": {"action": "approve", "reviewer": "x"}}
        if path == "/extract":
            kw = {"files": {"files": ("a.png", b"x")}}
        if path == "/extract_json":
            kw = {"json": {"files": [{"filename": "a", "content_base64": "eA=="}]}}
        assert getattr(client, m)(path, **kw).status_code == 401, path
    assert client.get("/health").status_code == 200
