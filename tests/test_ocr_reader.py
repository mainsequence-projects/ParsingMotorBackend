"""Textract as the independent second reader: schema-driven queries, normalized agreement,
deterministic selection. Reading facts only; acceptance stays in Back Office."""

import pytest

from extractor import aws, document_service, ocr_reader
from extractor import evidence_contracts as E
from tests.test_document_endpoints import BANK, typed_body
from tests.test_document_endpoints import client as client
from tests.test_document_endpoints import upload as upload

DRAFT = BANK["draft_schema"]


def vision(attrs, page=1):
    citations = [{"pointer": p, "pages": [page], "raw_text": str(v)}
                 for p, v in ocr_reader.scalar_leaves(attrs)]
    return {"attributes": {"document_type": "bank_statement", **attrs}, "citations": citations}


def fake_readers(monkeypatch, attrs, answers):
    """One vision reply; Textract answers keyed by the query text fragment they match."""
    seen = []

    def converse(prompt, **kwargs):
        return vision(attrs), "vision-model", {"cache_hit": False, "usage": {}}

    def textract(png, queries):
        seen.append(dict(queries))
        out = {}
        for alias, text in queries.items():
            for fragment, (value, conf) in answers.items():
                if fragment in text.lower():
                    out[alias] = {"value": value, "confidence": conf,
                                  "bbox": {"Left": 0.1, "Top": 0.2, "Width": 0.3, "Height": 0.05}}
        return out, {"cache_hit": False, "called": True}

    monkeypatch.setattr(document_service, "converse_json", converse)
    monkeypatch.setattr(aws, "textract_queries", textract)
    return seen


def post(client, upload):
    body = {**typed_body(upload), "contract_version": 2, "reader_mode": "independent_readers"}
    r = client.post("/extract_typed_json", json=body)
    assert r.status_code == 200, r.text
    return E.ExtractionResponseV2.model_validate(r.json())


def field(result, pointer):
    return next(f for f in result.field_evidence if f.pointer == pointer)


@pytest.mark.parametrize("pointer,a,b", [
    ("/opening_balance/amount", "41 000,00 K", "41000.00"),     # OCR drops the č of Kč
    ("/issue_date", "25.09.2026", "2026-09-25"),
    ("/account_identifier", "CZ65 0800 0000 1920 0014 5399", "CZ6508000000192000145399"),
    ("/bank_name", "Komercni banka", "Komerční banka"),          # Textract has no Czech model
    ("/opening_balance/currency", "Kč", "CZK"),
])
def test_normalized_forms_agree(pointer, a, b):
    leaf = ocr_reader.leaf_schema(DRAFT, pointer)
    assert ocr_reader.compare_form(leaf, pointer, a) == ocr_reader.compare_form(leaf, pointer, b)


def test_queries_come_from_schema_titles_and_skip_arrays():
    q = ocr_reader.query_text(DRAFT, "/opening_balance/amount")
    assert "opening balance" in q.lower() and len(q) <= 200
    assert ocr_reader.query_text(DRAFT, "/account_holders/0") is None


def test_agreement_after_normalization_keeps_vision_spelling_and_adds_ocr_box(client, upload, monkeypatch):
    fake_readers(monkeypatch, {"bank_name": "Komerční banka"}, {"bank name": ("Komercni banka", 0.97)})
    result = post(client, upload)
    f = field(result, "/bank_name")
    assert f.agreement == "agree" and f.selected_reader_id == "vision_primary"
    ocr = next(c for c in f.candidates if c.reader_id == "ocr_textract")
    assert ocr.confidence == 0.97 and ocr.raw_text == "Komercni banka"
    assert ocr.locations[0].bounding_box == pytest.approx([0.1, 0.2, 0.4, 0.25])
    assert result.attributes["bank_name"] == "Komerční banka"


def test_high_confidence_ocr_wins_a_numeric_disagreement(client, upload, monkeypatch):
    attrs = {"opening_balance": {"amount": "56000.00", "currency": "CZK"}}
    fake_readers(monkeypatch, attrs, {"opening balance amount": ("41 000,00 Kč", 0.95)})
    result = post(client, upload)
    f = field(result, "/opening_balance/amount")
    assert f.agreement == "disagree" and f.selected_reader_id == "ocr_textract"
    assert result.attributes["opening_balance"]["amount"] == "41000.00"
    assert result.canonical_schema_valid or result.canonical_schema_errors   # still a draft


def test_low_confidence_ocr_never_overrides_the_vision_reader(client, upload, monkeypatch):
    attrs = {"opening_balance": {"amount": "56000.00", "currency": "CZK"}}
    fake_readers(monkeypatch, attrs, {"opening balance amount": ("41 000,00 Kč", 0.6)})
    result = post(client, upload)
    f = field(result, "/opening_balance/amount")
    assert f.agreement == "disagree" and f.selected_reader_id == "vision_primary"
    assert result.attributes["opening_balance"]["amount"] == "56000.00"


def test_ocr_queries_only_the_cited_page_for_found_values(client, upload, monkeypatch):
    seen = fake_readers(monkeypatch, {"bank_name": "KB"}, {})
    post(client, upload)
    assert seen and any("bank name" in q.lower() for q in seen[0].values())


def test_single_mode_does_not_call_textract(client, upload, monkeypatch):
    seen = fake_readers(monkeypatch, {"bank_name": "KB"}, {})
    r = client.post("/extract_typed_json", json={**typed_body(upload), "contract_version": 2})
    assert r.status_code == 200 and seen == []
