"""Offline HTTP and provider-boundary tests using actual Back Office schema snapshots."""

import base64
import copy
import io
import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from extractor import api, document_service, pipeline
from extractor.cost import BudgetExceeded
from extractor.document_models import ExtractTypedJsonRequest
from extractor.schema_contract import schema_hash

CONTRACTS = json.loads(
    (Path(__file__).parent / "fixtures/backoffice_canonical_contracts.json").read_text()
)
BANK = next(c for c in CONTRACTS if c["document_type"] == "bank_statement")


@pytest.fixture
def upload():
    out = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(out, "PNG")
    return {
        "filename": "statement.png",
        "content_base64": base64.b64encode(out.getvalue()).decode(),
    }


@pytest.fixture
def client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected AWS/legacy pipeline call")

    monkeypatch.setattr(pipeline, "run_package", forbidden)
    monkeypatch.setattr(pipeline, "classify_pages", forbidden)
    monkeypatch.setattr(document_service, "converse_json", forbidden)
    monkeypatch.setattr(api, "API_KEY", "")
    return TestClient(api.app)


def typed_body(upload, contract=BANK):
    return copy.deepcopy(
        {
            "files": [upload],
            "document_type": contract["document_type"],
            "schema_version": contract["version"],
            "schema_hash": contract["content_sha256"],
            "canonical_schema": contract["canonical_schema"],
            "output_schema": contract["draft_schema"],
        }
    )


def discovery_body(upload):
    return {
        "files": [copy.deepcopy(upload)],
        "document_types": [
            {
                "document_type": "bank_statement",
                "description": "Bank account statement with balances",
            },
            {"document_type": "passport", "description": "Travel document identifying its holder"},
        ],
    }


def reply(monkeypatch, data):
    def fake(*args, **kwargs):
        return data, "test-model", {"cache_hit": False}

    monkeypatch.setattr(document_service, "converse_json", fake)


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c["document_type"])
def test_all_actual_canonical_contracts_accepted(contract, upload):
    request = ExtractTypedJsonRequest.model_validate(typed_body(upload, contract))
    assert request.output_schema == contract["draft_schema"]
    assert request.canonical_schema == contract["canonical_schema"]


def test_discovery_only_uses_supplied_catalog(client, upload, monkeypatch):
    seen = []

    def fake(prompt, **kwargs):
        seen.append((prompt, kwargs))
        return (
            {"document_type": "passport", "confidence": 0.9, "reason": "Travel document"},
            "classifier",
            {},
        )

    monkeypatch.setattr(document_service, "converse_json", fake)

    def forbidden(*args, **kwargs):
        pytest.fail("discovery must never extract")

    monkeypatch.setattr(document_service, "extract_typed", forbidden)
    r = client.post("/classify_json", json=discovery_body(upload))
    assert r.status_code == 200, r.text
    assert len(seen) == 1
    assert "passport" in seen[0][0] and "Travel document identifying its holder" in seen[0][0]
    assert "model_id" in seen[0][1]
    page = r.json()["pages"][0]
    assert page["document_type"] == "passport" and page["page"] == 1
    assert len(page["source_sha256"]) == 64
    assert "attributes" not in page and "run_id" not in r.json()


def test_unknown_discovery_is_explicit_null(client, upload, monkeypatch):
    reply(monkeypatch, {"document_type": None, "confidence": 0.2, "reason": "Unrecognized"})
    r = client.post("/classify_json", json=discovery_body(upload))
    assert r.status_code == 200 and r.json()["pages"][0]["document_type"] is None


@pytest.mark.parametrize(
    "data",
    [
        {"document_type": "invented", "confidence": 0.9, "reason": "x"},
        {"document_type": "passport", "confidence": "0.9", "reason": "x"},
        {"document_type": "passport", "confidence": 9, "reason": "x"},
        {"document_type": "passport", "confidence": 0.9, "reason": "x", "attributes": {}},
    ],
)
def test_invalid_classifier_response_is_upstream_error(client, upload, monkeypatch, data):
    reply(monkeypatch, data)
    assert client.post("/classify_json", json=discovery_body(upload)).status_code == 502


def test_typed_extraction_receives_full_nested_schema_and_skips_discovery(
    client, upload, monkeypatch
):
    body = typed_body(upload)
    attributes = {
        "document_type": "bank_statement",
        "account_holders": ["Synthetic Holder"],
        "bank_name": "Example",
        "account_identifier": "123456789",
        "statement_period": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        "currency": "EUR",
        "closing_balance": {"amount": "120.50", "currency": "EUR"},
    }

    def fake(prompt, **kwargs):
        assert json.dumps(body["canonical_schema"], ensure_ascii=False) in prompt
        assert json.dumps(body["output_schema"], ensure_ascii=False) in prompt
        assert "Do not classify" in prompt and "model_id" not in kwargs
        assert len(kwargs["images"]) == 1
        return attributes, "extractor", {"cache_hit": True}

    monkeypatch.setattr(document_service, "converse_json", fake)
    monkeypatch.setattr(document_service, "discover", lambda *a: pytest.fail("must skip discovery"))
    r = client.post("/extract_typed_json", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["attributes"] == attributes
    assert result["canonical_schema_valid"] and result["canonical_schema_errors"] == []
    assert result["schema_hash"] == BANK["content_sha256"]
    assert result["schema_version"] == 1 and result["status"] == "draft"
    assert result["cache_hit"] is True


def test_partial_extraction_is_draft_with_completeness_issues(client, upload, monkeypatch):
    reply(monkeypatch, {"document_type": "bank_statement", "currency": None})
    r = client.post("/extract_typed_json", json=typed_body(upload))
    assert r.status_code == 200, r.text
    assert not r.json()["canonical_schema_valid"]
    assert any(e["keyword"] == "required" for e in r.json()["canonical_schema_errors"])


@pytest.mark.parametrize(
    "attributes",
    [
        {"document_type": "passport"},
        {},
        [],
        {"document_type": "bank_statement", "unexpected": "secret"},
        {"document_type": "bank_statement", "statement_period": {"start_date": "not-a-date"}},
        {"document_type": "bank_statement", "statement_period": {"unexpected": 1}},
        {"document_type": "bank_statement", "account_holders": "not-an-array"},
        {"document_type": "bank_statement", "currency": "euro"},
    ],
)
def test_output_must_match_full_draft_schema(client, upload, monkeypatch, attributes):
    reply(monkeypatch, attributes)
    r = client.post("/extract_typed_json", json=typed_body(upload))
    assert r.status_code == 502, r.text
    assert "secret" not in r.text and "not-a-date" not in r.text


@pytest.mark.parametrize("path", ["/classify_json", "/extract_typed_json", "/extract_json"])
@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "file_extra",
        "filename_number",
        "base64_number",
        "bad_base64",
        "empty_files",
        "too_many_files",
    ],
)
def test_strict_envelopes(client, upload, path, mutation):
    body = (
        typed_body(upload)
        if path == "/extract_typed_json"
        else discovery_body(upload)
        if path == "/classify_json"
        else {"files": [copy.deepcopy(upload)]}
    )
    if mutation == "extra":
        body["ignored_option"] = True
    if mutation == "file_extra":
        body["files"][0]["url"] = "https://example.com"
    if mutation == "filename_number":
        body["files"][0]["filename"] = 123
    if mutation == "base64_number":
        body["files"][0]["content_base64"] = 123
    if mutation == "bad_base64":
        body["files"][0]["content_base64"] = "@@@"
    if mutation == "empty_files":
        body["files"] = []
    if mutation == "too_many_files":
        body["files"] *= 13
    r = client.post(path, json=body)
    assert r.status_code == 422, r.text
    assert upload["content_base64"] not in r.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "1"),
        ("schema_version", True),
        ("schema_version", 0),
        ("schema_hash", "0" * 64),
        ("document_type", "passport"),
    ],
)
def test_invalid_typed_metadata_rejected(client, upload, field, value):
    body = typed_body(upload)
    body[field] = value
    assert client.post("/extract_typed_json", json=body).status_code == 422


@pytest.mark.parametrize(
    "mutation",
    [
        "remote_ref",
        "unresolved_ref",
        "cyclic_ref",
        "invalid",
        "wrong_type",
        "id",
        "wrong_dialect",
        "oversized",
    ],
)
def test_bad_schemas_rejected_before_provider(client, upload, mutation):
    body = typed_body(upload)
    schema = body["output_schema"]
    if mutation == "remote_ref":
        schema["properties"]["x"] = {"$ref": "https://example.com/schema.json"}
    if mutation == "unresolved_ref":
        schema["properties"]["x"] = {"$ref": "#/$defs/NoSuchType"}
    if mutation == "cyclic_ref":
        schema["$defs"]["Cycle"] = {"$ref": "#/$defs/Cycle"}
    if mutation == "invalid":
        schema["required"] = 42
    if mutation == "wrong_type":
        schema["properties"]["document_type"]["const"] = "passport"
    if mutation == "id":
        schema["$id"] = "https://example.com/schema.json"
    if mutation == "wrong_dialect":
        schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    if mutation == "oversized":
        schema["description"] = "a" * 262145
    body["schema_hash"] = schema_hash(body["canonical_schema"], schema)
    r = client.post("/extract_typed_json", json=body)
    assert r.status_code == 422, r.text


def test_duplicate_types_and_legacy_string_bool_rejected(client, upload):
    body = discovery_body(upload)
    body["document_types"] *= 2
    assert client.post("/classify_json", json=body).status_code == 422
    assert (
        client.post("/extract_json", json={"files": [upload], "explain": "false"}).status_code
        == 422
    )


@pytest.mark.parametrize("path", ["/classify_json", "/extract_typed_json"])
def test_authentication_before_operations(client, upload, monkeypatch, path):
    monkeypatch.setattr(api, "API_KEY", "test-key")
    body = typed_body(upload) if path == "/extract_typed_json" else discovery_body(upload)
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"x-api-key": "wrong"}).status_code == 401
    reply(
        monkeypatch,
        {"document_type": "bank_statement"}
        if path == "/extract_typed_json"
        else {"document_type": None, "confidence": 0.0, "reason": "Unknown"},
    )
    assert client.post(path, json=body, headers={"x-api-key": "test-key"}).status_code == 200


@pytest.mark.parametrize("path", ["/classify_json", "/extract_typed_json"])
@pytest.mark.parametrize(
    "exception,code",
    [(RuntimeError("private ARN and document content"), 502), (BudgetExceeded("budget"), 503)],
)
def test_safe_upstream_errors(client, upload, monkeypatch, path, exception, code):
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(document_service, "converse_json", fail)
    body = typed_body(upload) if path == "/extract_typed_json" else discovery_body(upload)
    r = client.post(path, json=body)
    assert r.status_code == code and "private ARN" not in r.text


def test_all_pages_processed_and_files_named_safely(client, upload, monkeypatch):
    pdf = pymupdf.open()
    for i in range(7):
        pdf.new_page().insert_text((72, 72), f"Page {i + 1}")
    body = typed_body(upload)
    body["files"] = [
        {"filename": "../same.pdf", "content_base64": base64.b64encode(pdf.tobytes()).decode()},
        upload,
    ]

    def fake(prompt, **kwargs):
        assert len(kwargs["images"]) == 8  # legacy extractor silently used only six
        return {"document_type": "bank_statement"}, "model", {}

    monkeypatch.setattr(document_service, "converse_json", fake)
    r = client.post("/extract_typed_json", json=body)
    assert r.status_code == 200, r.text
    assert len(r.json()["pages"]) == 8
    assert r.json()["pages"][0]["source_file"] == "same.pdf"


@pytest.mark.parametrize("path", ["/classify_json", "/extract_typed_json"])
def test_invalid_file_and_page_limit_before_provider(client, upload, path):
    body = typed_body(upload) if path == "/extract_typed_json" else discovery_body(upload)
    body["files"] = [
        {"filename": "bad.pdf", "content_base64": base64.b64encode(b"%PDF-invalid").decode()}
    ]
    assert client.post(path, json=body).status_code == 422
    pdf = pymupdf.open()
    for _ in range(21):
        pdf.new_page()
    body["files"][0]["content_base64"] = base64.b64encode(pdf.tobytes()).decode()
    assert client.post(path, json=body).status_code == 413


def test_json_request_schema_is_generated_from_pydantic(client):
    spec = client.get("/openapi.json").json()
    for path in ["/classify_json", "/extract_typed_json", "/extract_json"]:
        schema = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        model = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
        assert model["additionalProperties"] is False


@pytest.mark.parametrize(
    "field",
    [
        "files",
        "document_type",
        "schema_version",
        "schema_hash",
        "canonical_schema",
        "output_schema",
    ],
)
def test_explicit_contract_fields_are_required(client, upload, field):
    body = typed_body(upload)
    del body[field]
    assert client.post("/extract_typed_json", json=body).status_code == 422


@pytest.mark.parametrize("schema_field", ["canonical_schema", "output_schema"])
def test_boolean_discriminator_schema_is_422(client, upload, schema_field):
    body = typed_body(upload)
    body[schema_field]["properties"]["document_type"] = False
    body["schema_hash"] = schema_hash(body["canonical_schema"], body["output_schema"])
    assert client.post("/extract_typed_json", json=body).status_code == 422


@pytest.mark.parametrize(
    "format_name,value", [("date-time", "definitely-not-a-timestamp"), ("uuid", "not-a-uuid")]
)
def test_formats_are_checked_not_silently_ignored(client, upload, monkeypatch, format_name, value):
    body = typed_body(upload)
    for name in ("canonical_schema", "output_schema"):
        body[name]["properties"]["test_field"] = {"type": "string", "format": format_name}
    body["schema_hash"] = schema_hash(body["canonical_schema"], body["output_schema"])
    reply(monkeypatch, {"document_type": "bank_statement", "test_field": value})
    assert client.post("/extract_typed_json", json=body).status_code == 502


def test_unknown_format_rejected(client, upload):
    body = typed_body(upload)
    body["output_schema"]["properties"]["test"] = {"type": "string", "format": "unsupported-format"}
    body["schema_hash"] = schema_hash(body["canonical_schema"], body["output_schema"])
    assert client.post("/extract_typed_json", json=body).status_code == 422
