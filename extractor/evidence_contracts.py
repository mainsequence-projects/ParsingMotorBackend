"""Opt-in v2 parser contracts: document observations, never business decisions.

Shared wire contract snapshot; no business policy is executed here.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .evidence_base import (
    MAX_PAGES,
    ClassifiedPage,
    ClassifyRequest,
    ExtractionResponse,
    ExtractRequest,
    SourcePage,
    StrictModel,
)

Unit = Annotated[float, Field(ge=0, le=1)]
PageNumber = Annotated[int, Field(ge=1, le=MAX_PAGES)]


class ClassifyRequestV2(ClassifyRequest):
    contract_version: Literal[2] = Field(
        description="Explicit opt-in to the evidence contract; never inferred by the provider."
    )

    @model_validator(mode="after")
    def valid_upload(self) -> Self:
        from .document_models import ClassifyJsonRequest

        ClassifyJsonRequest.model_validate(self.model_dump(exclude={"contract_version"}))
        return self


class ExtractRequestV2(ExtractRequest):
    contract_version: Literal[2] = Field(
        description="Explicit evidence contract version; never inferred or silently downgraded."
    )
    reader_mode: Literal["single", "independent_readers"] = Field(
        default="single",
        description="Requested reading strategy, not a business acceptance policy.",
    )
    page_selection: list[PageNumber] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PAGES,
        description="Ordered physical pages forming one logical document in the supplied original file; null selects all pages. Original page numbers must survive extraction.",
    )

    @model_validator(mode="after")
    def unique_pages(self) -> Self:
        from .document_models import ExtractTypedJsonRequest

        ExtractTypedJsonRequest.model_validate(
            self.model_dump(exclude={"contract_version", "reader_mode", "page_selection"})
        )
        if self.page_selection is not None and len(set(self.page_selection)) != len(
            self.page_selection
        ):
            raise ValueError("page_selection must not repeat a physical page")
        return self


class PageObservation(ClassifiedPage):
    printed_page_number: int | None = Field(
        default=None,
        ge=1,
        description="Page number printed on the document; distinct from physical source page.",
    )
    printed_page_count: int | None = Field(
        default=None,
        ge=1,
        description="Total page count printed on the source, not an assertion that all pages were received.",
    )
    rotation_degrees: Literal[0, 90, 180, 270] = Field(
        default=0,
        description="Clockwise rotation applied to the source page to obtain the upright evidence coordinate space.",
    )
    group_hint: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Source-local grouping observation; Back Office decides document membership and ownership.",
    )
    boundary: Literal["starts_document", "continues_document", "unknown"] = Field(
        default="unknown",
        description="Observed document boundary; unknown requires grouping resolution by Back Office.",
    )

    @model_validator(mode="after")
    def printed_pagination(self) -> Self:
        if (
            self.printed_page_number
            and self.printed_page_count
            and self.printed_page_number > self.printed_page_count
        ):
            raise ValueError("printed_page_number exceeds printed_page_count")
        return self


class ProviderUsage(StrictModel):
    model: str = Field(
        min_length=1,
        max_length=200,
        description="Actual provider/model version used for this operation.",
    )
    input_tokens: int | None = Field(
        default=None, ge=0, description="Provider-reported input tokens; null when unavailable."
    )
    output_tokens: int | None = Field(
        default=None, ge=0, description="Provider-reported output tokens; null when unavailable."
    )
    duration_ms: int | None = Field(
        default=None,
        ge=0,
        description="Measured provider duration in milliseconds; null when unavailable.",
    )


def page_keys(pages: list[SourcePage]) -> set[tuple[str, int]]:
    keys = {(page.source_file, page.page) for page in pages}
    if len(keys) != len(pages):
        raise ValueError("source manifest contains duplicate physical pages")
    hashes = {}
    for page in pages:
        if page.source_file in hashes and hashes[page.source_file] != page.source_sha256:
            raise ValueError("source manifest contains conflicting file hashes")
        hashes[page.source_file] = page.source_sha256
    return keys


class ClassificationResponseV2(StrictModel):
    contract_version: Literal[2]
    pages: list[PageObservation] = Field(min_length=1, max_length=MAX_PAGES)
    usage: list[ProviderUsage] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_manifest(self) -> Self:
        page_keys(self.pages)
        return self


class EvidenceLocation(StrictModel):
    source_file: str = Field(
        min_length=1, description="Filename in this response's immutable source-page manifest."
    )
    page: PageNumber = Field(
        description="Physical page number in the original source, never a printed or regrouped page number."
    )
    bounding_box: list[Unit] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="Normalized [left, top, right, bottom] on the rendered page after the recorded rotation; null means page-level evidence only.",
    )

    @model_validator(mode="after")
    def ordered_box(self) -> Self:
        if self.bounding_box is not None:
            left, top, right, bottom = self.bounding_box
            if left >= right or top >= bottom:
                raise ValueError("bounding_box must have positive width and height")
        return self


class ExtractionPage(SourcePage):
    rotation_degrees: Literal[0, 90, 180, 270] = Field(
        default=0, description="Clockwise rotation used for the upright evidence coordinates."
    )


class ReaderCandidate(StrictModel):
    reader_id: str = Field(
        min_length=1,
        max_length=120,
        description="Unique reader identifier within this field, such as textract or vision.",
    )
    model: str = Field(
        min_length=1,
        max_length=200,
        description="Actual reader model/version; not a reviewer identity.",
    )
    value: JsonValue = Field(
        description="Reader's normalized extraction candidate; null means unreadable or absent."
    )
    raw_text: str | None = Field(
        default=None,
        max_length=10000,
        description="Reader's source transcription when available; no business interpretation.",
    )
    confidence: Unit | None = Field(
        default=None,
        description="Provider-reported extraction confidence for this reading; null if unavailable, never a classification score.",
    )
    locations: list[EvidenceLocation] = Field(
        default_factory=list,
        max_length=100,
        description="Source locations supporting this reader's candidate.",
    )

    @model_validator(mode="after")
    def grounded_value(self) -> Self:
        if self.value is not None and not self.locations:
            raise ValueError("a non-null candidate requires source evidence")
        return self


class FieldEvidence(StrictModel):
    pointer: str = Field(
        min_length=1,
        max_length=500,
        description="RFC 6901 JSON Pointer into attributes, including array indices; bound to this extraction revision.",
    )
    candidates: list[ReaderCandidate] = Field(min_length=1, max_length=8)
    selected_reader_id: str | None = Field(
        default=None,
        description="Reader supplying the draft attribute; null when unresolved. Selection is not acceptance.",
    )
    agreement: Literal["single_reader", "agree", "disagree", "missing"] = Field(
        description="Exact agreement of normalized candidates within this document; no business tolerance or review status."
    )

    @model_validator(mode="after")
    def consistent_readings(self) -> Self:
        if not self.pointer.startswith("/") or re.search(r"~(?![01])", self.pointer):
            raise ValueError("pointer must be a valid non-root JSON Pointer")
        readers = {candidate.reader_id for candidate in self.candidates}
        if len(readers) != len(self.candidates):
            raise ValueError("reader identifiers must be unique per field")
        if self.selected_reader_id is not None and self.selected_reader_id not in readers:
            raise ValueError("selected reader has no candidate")
        values = [candidate.value for candidate in self.candidates if candidate.value is not None]
        actual = (
            "missing"
            if not values
            else "single_reader"
            if len(values) == 1
            else "agree"
            if all(value == values[0] for value in values)
            else "disagree"
        )
        if self.agreement != actual:
            raise ValueError("agreement does not describe the candidate values")
        return self


def pointer_value(attributes: dict, pointer: str):
    value = attributes
    for encoded in pointer[1:].split("/"):
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(value):
                raise ValueError("pointer does not identify an extracted array item")
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ValueError("pointer does not identify an extracted attribute")
    return value


class ExtractionResponseV2(ExtractionResponse):
    contract_version: Literal[2]
    pages: list[ExtractionPage] = Field(min_length=1, max_length=MAX_PAGES)
    normalization_version: str = Field(
        min_length=1,
        max_length=120,
        description="Version of extraction normalization, independent of any Back Office business policy.",
    )
    field_evidence: list[FieldEvidence] = Field(max_length=5000)
    evidence_complete: bool = Field(
        description="True only when every non-null attribute leaf except document_type has field evidence; not extraction accuracy or document completeness."
    )
    usage: list[ProviderUsage] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        manifest = page_keys(self.pages)
        pointers = set()
        for field in self.field_evidence:
            if field.pointer in pointers:
                raise ValueError("duplicate field evidence pointer")
            pointers.add(field.pointer)
            value = pointer_value(self.attributes, field.pointer)
            selected = next(
                (c for c in field.candidates if c.reader_id == field.selected_reader_id), None
            )
            if selected is None and value is not None:
                raise ValueError("a non-null attribute requires a selected reader")
            if selected is not None and value != selected.value:
                raise ValueError("selected candidate does not match the draft attribute")
            for candidate in field.candidates:
                if any(
                    (location.source_file, location.page) not in manifest
                    for location in candidate.locations
                ):
                    raise ValueError("field evidence references a page outside the source manifest")
        if self.evidence_complete:

            def leaves(value, pointer=""):
                if isinstance(value, dict) and value:
                    for key, item in value.items():
                        yield from leaves(
                            item, pointer + "/" + key.replace("~", "~0").replace("/", "~1")
                        )
                elif isinstance(value, list) and value:
                    for index, item in enumerate(value):
                        yield from leaves(item, f"{pointer}/{index}")
                elif value is not None and pointer != "/document_type":
                    yield pointer

            if set(leaves(self.attributes)) - pointers:
                raise ValueError(
                    "evidence_complete requires evidence for every non-null attribute leaf"
                )
        return self


EVIDENCE_CONTRACTS = (
    ClassifyRequestV2,
    ClassificationResponseV2,
    ExtractRequestV2,
    ExtractionResponseV2,
)
