"""Extraction API metadata and its checked-in OpenAPI contract."""

import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from extractor import __version__, api


def test_health_reports_extraction_limits_without_business_policy():
    response = TestClient(api.app).get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == __version__
    assert data["limits"]["max_pages"] >= 1
    assert not {"rules_version", "rules_error", "audit_storage"} & data.keys()


def test_openapi_documents_only_supported_operations():
    spec = TestClient(api.app).get("/openapi.json").json()
    assert set(spec["paths"]) == {"/health", "/session", "/classify_json", "/extract_typed_json", "/compare_documents_json"}
    assert json.loads((Path(__file__).parents[1] / "docs/openapi.json").read_text()) == spec
    for name in ("ExtractResponse", "DecisionRequest", "Evaluation", "Check", "RunState"):
        assert name not in spec["components"]["schemas"]


def test_cors_is_opt_in(monkeypatch):
    try:
        monkeypatch.delenv("EXTRACTOR_CORS_ORIGINS", raising=False)
        importlib.reload(api)
        assert "access-control-allow-origin" not in TestClient(api.app).get(
            "/health", headers={"Origin": "http://localhost:5173"}
        ).headers
        monkeypatch.setenv("EXTRACTOR_CORS_ORIGINS", "http://localhost:5173")
        importlib.reload(api)
        response = TestClient(api.app).options("/extract_typed_json", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        })
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    finally:
        monkeypatch.delenv("EXTRACTOR_CORS_ORIGINS", raising=False)
        importlib.reload(api)
