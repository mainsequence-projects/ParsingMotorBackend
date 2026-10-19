"""V2 HTTP contracts preserve source evidence and never invoke loan decisions."""

import json
from pathlib import Path

import pymupdf
import pytest

from extractor import aws, document_service
from extractor import evidence_contracts as E
from tests.test_document_endpoints import client as client
from tests.test_document_endpoints import discovery_body, typed_body
from tests.test_document_endpoints import upload as upload


@pytest.mark.parametrize("model", E.EVIDENCE_CONTRACTS)
def test_contract_matches_back_office_snapshot(model):
    expected = json.loads(
        (Path(__file__).parent / "fixtures/processing_v2" / f"{model.__name__}.json").read_text()
    )
    assert model.model_json_schema() == expected


def reading(name="Bank name", page=1):
    return {
        "attributes": {"document_type": "bank_statement", "bank_name": name},
        "citations": [{"pointer": "/bank_name", "pages": [page], "raw_text": name}],
    }


def replies(monkeypatch, values):
    calls = []

    def fake(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return (
            values[len(calls) - 1],
            f"model-{len(calls)}",
            {"cache_hit": False, "usage": {"input_tokens": 12, "output_tokens": 6}},
        )

    monkeypatch.setattr(document_service, "converse_json", fake)
    return calls


def test_classification_v2_preserves_printed_pagination_but_hashes_are_server_owned(
    client, upload, monkeypatch
):
    body = {**discovery_body(upload), "contract_version": 2}
    replies(
        monkeypatch,
        [
            {
                "document_type": "bank_statement",
                "confidence": 0.91,
                "reason": "Observed heading",
                "printed_page_number": 2,
                "printed_page_count": 3,
                "group_hint": "Statement A",
                "boundary": "continues_document",
            }
        ],
    )
    response = client.post("/classify_json", json=body)
    assert response.status_code == 200, response.text
    data = E.ClassificationResponseV2.model_validate(response.json())
    assert data.pages[0].page == 1 and data.pages[0].printed_page_number == 2
    assert len(data.pages[0].source_sha256) == 64
    assert data.usage[0].input_tokens == 12
    assert "recommended_next_step" not in response.json()


def test_extraction_v2_returns_citations_without_inventing_ocr_scores_or_boxes(
    client, upload, monkeypatch
):
    body = {**typed_body(upload), "contract_version": 2}
    replies(monkeypatch, [reading()])
    response = client.post("/extract_typed_json", json=body)
    assert response.status_code == 200, response.text
    result = E.ExtractionResponseV2.model_validate(response.json())
    assert result.status == "draft" and not result.canonical_schema_valid
    assert result.evidence_complete
    candidate = result.field_evidence[0].candidates[0]
    assert candidate.confidence is None and candidate.locations[0].bounding_box is None
    assert result.field_evidence[0].pointer == "/bank_name"


def textract(monkeypatch, value, conf=0.5):
    seen = []

    def fake(png, queries):
        seen.append(dict(queries))
        return ({a: {"value": value, "confidence": conf, "bbox": None} for a in queries},
                {"cache_hit": False, "called": True})

    monkeypatch.setattr(aws, "textract_queries", fake)
    return seen


def test_independent_readers_preserve_disagreement_without_resolving_it(
    client, upload, monkeypatch
):
    body = {**typed_body(upload), "contract_version": 2, "reader_mode": "independent_readers"}
    replies(monkeypatch, [reading("Primary Bank")])
    seen = textract(monkeypatch, "Secondary Bank")
    response = client.post("/extract_typed_json", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["field_evidence"][0]["agreement"] == "disagree"
    assert result["attributes"]["bank_name"] == "Primary Bank"   # text: OCR never overrides
    assert len(result["field_evidence"][0]["candidates"]) == 2
    assert "Primary Bank" not in str(seen)  # The OCR reader never sees the vision output.
    assert "policy" not in result and "checks" not in result


@pytest.mark.parametrize(
    "bad", ["bad_page", "extra_decision", "wrong_pointer", "duplicate"]
)
def test_bad_provider_evidence_fails_closed(client, upload, monkeypatch, bad):
    data = reading()
    if bad == "missing_citation":
        data["citations"] = []
    elif bad == "bad_page":
        data["citations"][0]["pages"] = [2]
    elif bad == "extra_decision":
        data["accepted"] = True
    elif bad == "wrong_pointer":
        data["citations"][0]["pointer"] = "/unrelated"
    else:
        data["citations"] *= 2
    replies(monkeypatch, [data])
    response = client.post(
        "/extract_typed_json", json={**typed_body(upload), "contract_version": 2}
    )
    assert response.status_code == 502
    assert "Bank name" not in response.text


@pytest.mark.parametrize("bad", ["base64", "hash", "remote_schema", "version", "repeated_page"])
def test_v2_request_validated_before_provider(client, upload, bad):
    body = {**typed_body(upload), "contract_version": 2}
    if bad == "base64":
        body["files"][0]["content_base64"] = "!!!"
    elif bad == "hash":
        body["schema_hash"] = "0" * 64
    elif bad == "remote_schema":
        body["output_schema"]["$ref"] = "https://untrusted.example/schema"
    elif bad == "version":
        body["contract_version"] = 3
    else:
        body["page_selection"] = [1, 1]
    assert client.post("/extract_typed_json", json=body).status_code == 422


def test_page_selection_keeps_original_physical_numbers_and_reordering(client, upload, monkeypatch):
    import base64

    document = pymupdf.open()
    for i in range(3):
        page = document.new_page()
        page.insert_text((50, 50), str(i + 1))
    upload = {
        "filename": "statement.pdf",
        "content_base64": base64.b64encode(document.tobytes()).decode(),
    }
    calls = replies(monkeypatch, [reading(page=3)])
    response = client.post(
        "/extract_typed_json",
        json={**typed_body(upload), "contract_version": 2, "page_selection": [3, 1]},
    )
    assert response.status_code == 200, response.text
    assert [p["page"] for p in response.json()["pages"]] == [3, 1]
    assert len(calls[0][1]["images"]) == 2
    assert response.json()["field_evidence"][0]["candidates"][0]["locations"][0]["page"] == 3
    assert (
        client.post(
            "/extract_typed_json",
            json={**typed_body(upload), "contract_version": 2, "page_selection": [4]},
        ).status_code
        == 422
    )


def test_array_items_are_never_aligned_by_index(client, upload, monkeypatch):
    first = reading()
    first["attributes"]["account_holders"] = ["Eva Horáková"]
    first["citations"].append({"pointer": "/account_holders/0", "pages": [1], "raw_text": "Eva Horáková"})
    replies(monkeypatch, [first])
    textract(monkeypatch, "Bank name")
    response = client.post(
        "/extract_typed_json",
        json={**typed_body(upload), "contract_version": 2, "reader_mode": "independent_readers"},
    )
    assert response.status_code == 200, response.text
    holder = next(f for f in response.json()["field_evidence"] if f["pointer"] == "/account_holders/0")
    assert holder["agreement"] == "single_reader" and len(holder["candidates"]) == 1

def test_prompts_restate_the_reply_shape_last_and_classify_by_meaning(client, upload, monkeypatch):
    calls = replies(monkeypatch, [reading()])
    client.post("/extract_typed_json", json={**typed_body(upload), "contract_version": 2})
    prompt = calls[0][0]
    schema_at = prompt.index("\nOUTPUT_SCHEMA:")
    assert prompt.index("REPLY FORMAT") > schema_at and prompt.rindex('"attributes"') > schema_at
    calls = replies(monkeypatch, [{"document_type": "bank_statement", "confidence": 0.9, "reason": "x"}])
    client.post("/classify_json", json={**discovery_body(upload), "contract_version": 2})
    assert "any language" in calls[0][0] and "Catalog:" in calls[0][0]


def test_uncited_value_is_dropped_and_marks_evidence_incomplete(client, upload, monkeypatch):
    data = reading()
    data["citations"] = []
    replies(monkeypatch, [data])
    response = client.post("/extract_typed_json", json={**typed_body(upload), "contract_version": 2})
    assert response.status_code == 200, response.text
    result = E.ExtractionResponseV2.model_validate(response.json())
    assert result.evidence_complete is False
    assert result.attributes["bank_name"] is None          # unsupported reading = unread
    assert result.field_evidence[0].agreement == "missing"
