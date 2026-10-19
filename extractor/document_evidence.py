"""Versioned, schema-driven observations. No requirements, review or lending rules."""

import json
import time

from pydantic import Field, JsonValue

from . import document_models as D
from . import document_service as service
from . import evidence_contracts as E
from .config import BEDROCK_CLASSIFY_FALLBACKS, BEDROCK_CLASSIFY_MODEL
from .evidence_base import StrictModel
from .ingest import InputError
from .schema_contract import validator


class ObservedClassification(D.Classification):
    printed_page_number: int | None = Field(default=None, ge=1)
    printed_page_count: int | None = Field(default=None, ge=1)
    group_hint: str | None = Field(default=None, min_length=1, max_length=200)
    boundary: str = "unknown"


class Citation(StrictModel):
    pointer: str
    pages: list[int] = Field(min_length=1, max_length=20)
    raw_text: str | None = Field(default=None, max_length=10000)


class Reading(StrictModel):
    attributes: dict[str, JsonValue]
    citations: list[Citation] = Field(max_length=5000)


def usage(model, meta, started):
    counts = meta.get("usage") or {}
    return E.ProviderUsage(
        model=model,
        input_tokens=counts.get("input_tokens"),
        output_tokens=counts.get("output_tokens"),
        duration_ms=int((time.monotonic() - started) * 1000),
    ).model_dump()


def classify(request):
    # Reuse v1's byte/schema validators before any provider operation.
    validated = D.ClassifyJsonRequest.model_validate(
        request.model_dump(exclude={"contract_version"})
    )
    pages = service._pages(validated)
    if len(pages) > 20:
        raise InputError("v2 supports at most 20 source pages", status=413)
    catalog = [c.model_dump() for c in request.document_types]
    allowed = {c.document_type for c in request.document_types}
    prompt = (
        "Classify ONLY against this catalog. Do not extract attributes or make business decisions. "
        "Return document_type (null if unknown), confidence [0,1], reason, printed_page_number "
        "and printed_page_count (null unless printed), group_hint (a visible document identifier "
        "or null), boundary (starts_document, continues_document, or unknown). Grouping is only "
        "an observation. Do not guess missing pagination. Catalog: " + json.dumps(catalog)
    )
    result, costs = [], []
    for page in pages:
        started = time.monotonic()
        data, model, meta = service.converse_json(
            prompt,
            images=[page.png],
            system=service.SYSTEM,
            max_tokens=800,
            model_id=BEDROCK_CLASSIFY_MODEL,
            fallbacks=BEDROCK_CLASSIFY_FALLBACKS,
            return_meta=True,
        )
        observed = ObservedClassification.model_validate(data)
        if observed.document_type is not None and observed.document_type not in allowed:
            raise ValueError("classifier returned a type outside the supplied catalog")
        result.append(
            {
                **service._source(page),
                **observed.model_dump(),
                "model": model,
                "cache_hit": bool(meta.get("cache_hit", False)),
                "rotation_degrees": page.rotation_applied,
            }
        )
        costs.append(usage(model, meta, started))
    return E.ClassificationResponseV2(contract_version=2, pages=result, usage=costs).model_dump()


def leaves(value, pointer=""):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from leaves(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from leaves(item, f"{pointer}/{index}")
    elif pointer != "/document_type":
        yield pointer, value


def extract(request):
    validated = D.ExtractTypedJsonRequest.model_validate(
        request.model_dump(exclude={"contract_version", "reader_mode", "page_selection"})
    )
    all_pages = service._pages(validated)
    if len(all_pages) > 20:
        raise InputError("v2 supports at most 20 source pages", status=413)
    by_number = {p.page: p for p in all_pages}
    selected = request.page_selection or list(by_number)
    if any(n not in by_number for n in selected):
        raise InputError("page_selection references a page outside the source")
    pages = [by_number[n] for n in selected]
    manifest = [service._source(p) for p in pages]
    prompt = (
        f"Extract ONE document of explicit type {request.document_type!r}. Do not classify, "
        "compare different documents, apply acceptance thresholds, or make business decisions. "
        "Return exactly an object with attributes and citations. Attributes must conform to "
        "OUTPUT_SCHEMA; use null for unreadable values only when permitted. Include document_type. "
        "Citations is a list of {pointer: RFC6901 JSON Pointer, pages: [physical page numbers], "
        "raw_text: literal transcription or null}. Cite EVERY non-null leaf except document_type, "
        "including each array item, to the actual supplied source page. Never invent evidence. "
        "No confidence scores or bounding boxes: this reader does not measure them. "
        "Do not return hashes, statuses or policy decisions. Images follow this physical page order: "
        + json.dumps(selected)
        + "\nCANONICAL_SCHEMA: "
        + json.dumps(request.canonical_schema)
        + "\nOUTPUT_SCHEMA: "
        + json.dumps(request.output_schema)
    )
    readers, costs, cache_hits = [], [], []
    modes = ["vision_primary"]
    if request.reader_mode == "independent_readers":
        modes.append("vision_secondary")
    for reader_id in modes:
        started = time.monotonic()
        options = (
            {}
            if reader_id == "vision_primary"
            else {"model_id": BEDROCK_CLASSIFY_MODEL, "fallbacks": []}
        )
        data, model, meta = service.converse_json(
            prompt + f"\nReading identity: {reader_id}. Read the source independently.",
            images=[p.png for p in pages],
            system=service.SYSTEM,
            max_tokens=16384,
            return_meta=True,
            **options,
        )
        if readers and model == readers[0][1]:
            raise ValueError("independent readers require distinct configured models")
        reading = Reading.model_validate(data)
        validator(request.output_schema).validate(reading.attributes)
        if reading.attributes.get("document_type") != request.document_type:
            raise ValueError("extraction changed the explicit document type")
        values = dict(leaves(reading.attributes))
        citations = {}
        for citation in reading.citations:
            if (
                citation.pointer in citations
                or citation.pointer not in values
                or len(set(citation.pages)) != len(citation.pages)
                or any(p not in selected for p in citation.pages)
            ):
                raise ValueError("invalid or duplicate field citation")
            citations[citation.pointer] = citation
        if any(v is not None and p not in citations for p, v in values.items()):
            raise ValueError("reader omitted evidence for an extracted value")
        # Different array shapes cannot safely be aligned by index. Fail closed, never discard
        # an unmatched reader value or pretend records in differently shaped arrays agree.
        if readers and values.keys() != readers[0][2].keys():
            raise ValueError("independent reader structures require alignment")
        readers.append((reader_id, model, values, citations, reading.attributes))
        costs.append(usage(model, meta, started))
        cache_hits.append(bool(meta.get("cache_hit", False)))
    fields = []
    for pointer, value in readers[0][2].items():
        candidates = []
        for reader_id, model, values, citations, _ in readers:
            citation = citations.get(pointer)
            candidates.append(
                E.ReaderCandidate(
                    reader_id=reader_id,
                    model=model,
                    value=values[pointer],
                    raw_text=citation.raw_text if citation else None,
                    confidence=None,
                    locations=[
                        E.EvidenceLocation(source_file=pages[0].source_file, page=p)
                        for p in citation.pages
                    ]
                    if citation
                    else [],
                )
            )
        present = [c.value for c in candidates if c.value is not None]
        agreement = (
            "missing"
            if not present
            else "single_reader"
            if len(present) == 1
            else "agree"
            if all(v == present[0] for v in present)
            else "disagree"
        )
        fields.append(
            E.FieldEvidence(
                pointer=pointer,
                candidates=candidates,
                selected_reader_id="vision_primary" if value is not None else None,
                agreement=agreement,
            )
        )
    attributes = readers[0][4]
    issues = [
        {"path": list(e.absolute_path), "keyword": str(e.validator)}
        for e in validator(request.canonical_schema).iter_errors(attributes)
    ]
    return E.ExtractionResponseV2(
        contract_version=2,
        document_type=request.document_type,
        schema_version=request.schema_version,
        schema_hash=request.schema_hash,
        status="draft",
        attributes=attributes,
        canonical_schema_valid=not issues,
        canonical_schema_errors=issues,
        pages=[{**p, "rotation_degrees": by_number[p["page"]].rotation_applied} for p in manifest],
        model=readers[0][1],
        cache_hit=all(cache_hits),
        normalization_version="schema_vision_v1",
        field_evidence=fields,
        evidence_complete=True,
        usage=costs,
    ).model_dump()
