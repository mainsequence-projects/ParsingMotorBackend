"""Versioned, schema-driven observations. No requirements, review or lending rules."""

import copy
import json
import time

from pydantic import Field, JsonValue

from . import document_models as D
from . import document_service as service
from . import evidence_contracts as E
from . import ocr_reader
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
    started = time.monotonic()
    data, model, meta = service.converse_json(
        prompt + "\nReading identity: vision_primary. Read the source independently.",
        images=[p.png for p in pages],
        system=service.SYSTEM,
        max_tokens=16384,
        return_meta=True,
    )
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
    costs, cache_hits = [usage(model, meta, started)], [bool(meta.get("cache_hit", False))]
    # Second reader = Textract (a different modality: OCR with its own confidence and box),
    # so the two readers do not share the same failure mode. It never sees the vision values.
    independent = request.reader_mode == "independent_readers"
    ocr = {}
    if independent:
        started = time.monotonic()
        ocr = ocr_reader.read(pages, request.output_schema, values, citations)
        costs.append(E.ProviderUsage(model=ocr_reader.MODEL,
                                     duration_ms=int((time.monotonic() - started) * 1000)))
    attributes = copy.deepcopy(reading.attributes)
    fields = []
    for pointer, value in values.items():
        leaf = ocr_reader.leaf_schema(request.output_schema, pointer)
        citation = citations.get(pointer)
        candidates = [
            E.ReaderCandidate(
                reader_id="vision_primary",
                model=model,
                value=value,
                raw_text=citation.raw_text if citation else None,
                confidence=None,
                locations=[
                    E.EvidenceLocation(source_file=pages[0].source_file, page=p)
                    for p in citation.pages
                ]
                if citation
                else [],
            )
        ]
        hit = ocr.get(pointer)
        if independent and leaf is not None:
            candidates.append(
                E.ReaderCandidate(
                    reader_id=ocr_reader.READER_ID,
                    model=ocr_reader.MODEL,
                    value=ocr_reader.candidate_value(leaf, pointer, value, hit["value"]) if hit else None,
                    raw_text=hit["value"] if hit else None,
                    confidence=hit["confidence"] if hit else None,
                    locations=[
                        E.EvidenceLocation(source_file=hit["source_file"], page=hit["page"],
                                           bounding_box=ocr_reader.box(hit.get("bbox")))
                    ]
                    if hit
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
        chosen, ocr_value = ocr_reader.select(leaf, pointer, value, hit, agreement)
        if chosen == ocr_reader.READER_ID:
            ocr_reader.set_pointer(attributes, pointer, ocr_value)
        fields.append(
            E.FieldEvidence(
                pointer=pointer,
                candidates=candidates,
                selected_reader_id=chosen,
                agreement=agreement,
            )
        )
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
        model=model,
        cache_hit=all(cache_hits),
        normalization_version=ocr_reader.NORMALIZATION_VERSION,
        field_evidence=fields,
        evidence_complete=True,
        usage=costs,
    ).model_dump()
