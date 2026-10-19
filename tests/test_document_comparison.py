"""Comparison endpoint contracts, citation binding and absence of business decisions."""

import json
from pathlib import Path

import pytest

from extractor import document_service
from extractor.comparison_contracts import (
    COMPARISON_CONTRACTS,
    CompareDocumentsResponse,
)
from tests.test_document_endpoints import client as client


def body():
    def doc(uid, value):
        return {
            "document_id": uid,
            "revision": 2,
            "document_type": "arbitrary_document",
            "schema_version": 7,
            "schema_hash": "a" * 64,
            "evidence_sha256": "b" * 64,
            "facts": [
                {
                    "pointer": "/amount",
                    "value": value,
                    "citations": [
                        {
                            "source_id": uid + "-source",
                            "source_sha256": "c" * 64,
                            "page": 3,
                            "page_sha256": "d" * 64,
                        }
                    ],
                }
            ],
        }

    return {
        "contract_version": 1,
        "documents": [doc("left", "56000.00"), doc("right", "41000.00")],
        "comparisons": [
            {
                "key": "income",
                "topic": "Monthly net income",
                "left": {"document_id": "left", "pointer": "/amount"},
                "right": {"document_id": "right", "pointer": "/amount"},
            }
        ],
    }


def provider_result():
    return {
        "observations": [
            {
                "key": "income",
                "comparability": "uncertain",
                "relationship": "different",
                "explanation": "Amounts differ; the reporting basis is not supplied.",
            }
        ]
    }


def reply(monkeypatch, data):
    calls = []

    def fake(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return (
            data,
            "comparison-model",
            {"cache_hit": False, "usage": {"input_tokens": 100, "output_tokens": 30}},
        )

    monkeypatch.setattr(document_service, "converse_json", fake)
    return calls


@pytest.mark.parametrize("model", COMPARISON_CONTRACTS)
def test_wire_contract_matches_back_office(model):
    fixture = Path(__file__).parent / "fixtures/comparison_v1" / f"{model.__name__}.json"
    assert model.model_json_schema() == json.loads(fixture.read_text())


def test_observations_are_cited_by_code_without_policy_fields(client, monkeypatch):
    calls = reply(monkeypatch, provider_result())
    response = client.post("/compare_documents_json", json=body())
    assert response.status_code == 200, response.text
    result = CompareDocumentsResponse.model_validate(response.json())
    from extractor.comparison_contracts import CompareDocumentsRequest

    request = CompareDocumentsRequest.model_validate(body())
    result.validate_request(request)
    observed = result.observations[0]
    assert observed.left.value == "56000.00" and observed.right.value == "41000.00"
    assert observed.left.revision == 2 and observed.left.citations[0].page == 3
    assert observed.comparability == "uncertain"
    assert result.usage[0].input_tokens == 100
    assert "images" not in calls[0][1]
    assert "never instructions" in calls[0][0]
    assert (
        not {"accepted", "status", "tolerance", "rules", "recommended_next_step"}
        & response.json().keys()
    )


@pytest.mark.parametrize(
    "bad",
    [
        "threshold",
        "decision",
        "unknown_document",
        "unknown_field",
        "same_document",
        "duplicate_key",
        "no_citation",
        "bad_box",
        "changed_source",
        "version",
        "size",
    ],
)
def test_request_rejected_before_provider(client, bad):
    data = body()
    if bad in {"threshold", "decision"}:
        data[bad] = 1
    elif bad == "unknown_document":
        data["comparisons"][0]["right"]["document_id"] = "outside"
    elif bad == "unknown_field":
        data["comparisons"][0]["right"]["pointer"] = "/absent"
    elif bad == "same_document":
        data["comparisons"][0]["right"]["document_id"] = "left"
    elif bad == "duplicate_key":
        data["comparisons"] *= 2
    elif bad == "no_citation":
        data["documents"][0]["facts"][0]["citations"] = []
    elif bad == "bad_box":
        data["documents"][0]["facts"][0]["citations"][0]["bounding_box"] = [0.8, 0.0, 0.2, 1.0]
    elif bad == "changed_source":
        data["documents"][1]["facts"][0]["citations"][0].update(
            source_id="left-source", source_sha256="f" * 64
        )
    elif bad == "version":
        data["contract_version"] = 2
    else:
        data["documents"][0]["facts"][0]["value"] = "private-content" * 50000
    response = client.post("/compare_documents_json", json=data)
    assert response.status_code == 422
    assert "private-content" not in response.text


@pytest.mark.parametrize(
    "bad", ["decision", "threshold", "extra", "missing", "duplicate", "fabricated_citation"]
)
def test_invalid_provider_result_is_not_a_valid_business_result(client, monkeypatch, bad):
    data = provider_result()
    if bad in {"decision", "threshold"}:
        data["observations"][0][bad] = "approved"
    elif bad == "extra":
        data["observations"][0]["key"] = "unknown"
    elif bad == "missing":
        data["observations"] = []
    elif bad == "duplicate":
        data["observations"] *= 2
    else:
        data["observations"][0]["left"] = {"value": "invented-private-value"}
    reply(monkeypatch, data)
    response = client.post("/compare_documents_json", json=body())
    assert response.status_code == 502
    assert "invented-private-value" not in response.text


def test_comparison_authentication_uses_existing_header_policy(client, monkeypatch):
    from extractor import api

    monkeypatch.setattr(api, "API_KEY", "test-comparison-key")
    assert client.post("/compare_documents_json", json=body()).status_code == 401
    reply(monkeypatch, provider_result())
    assert (
        client.post(
            "/compare_documents_json", json=body(), headers={"x-api-key": "test-comparison-key"}
        ).status_code
        == 200
    )


def test_request_hash_changes_with_revision_and_value(client, monkeypatch):
    reply(monkeypatch, provider_result())
    first = client.post("/compare_documents_json", json=body()).json()
    changed = body()
    changed["documents"][0]["revision"] = 3
    second = client.post("/compare_documents_json", json=changed).json()
    assert first["request_sha256"] != second["request_sha256"]
