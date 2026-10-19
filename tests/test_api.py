"""The parser exposes extraction only; application workflows belong to Back Office."""

import importlib.util

import pytest
from fastapi.testclient import TestClient

from extractor import api


@pytest.mark.parametrize("method,path", [
    ("post", "/extract"), ("post", "/extract_json"),
    ("get", "/runs/example"), ("post", "/runs/example/decisions"),
    ("get", "/runs/example/verify"), ("get", "/runs/example/protocol"),
    ("get", "/rules"), ("get", "/learning"),
])
def test_loan_workflow_endpoints_are_removed(method, path):
    assert getattr(TestClient(api.app), method)(path).status_code == 404


@pytest.mark.parametrize("module", [
    "pipeline", "evaluate", "cross_checks", "rules", "rules_engine", "audit", "protocol",
    "reconcile", "doclist", "classify", "extract",
])
def test_legacy_business_engine_is_not_importable(module):
    assert importlib.util.find_spec(f"extractor.{module}") is None


def test_root_opens_extraction_api_documentation():
    response = TestClient(api.app).get("/", follow_redirects=False)
    assert response.headers["location"] == "docs"


def test_token_only_in_header(monkeypatch):
    monkeypatch.setattr(api, "API_KEY", "token")
    client = TestClient(api.app)
    body = {"files": [{"filename": "a.png", "content_base64": "eA=="}],
            "document_types": [{"document_type": "id", "description": "Identification"}]}
    assert client.post("/classify_json", json=body).status_code == 401
    assert client.post("/classify_json?api_key=token", json=body).status_code == 401
    assert client.post("/classify_json", json=body, headers={"x-api-key": "wrong"}).status_code == 401
    assert client.get("/health").status_code == 200
