# Wire contract snapshot from Back Office; no runtime dependency on its business engine.
"""Strict wire contracts for the two ParsingMotor document operations."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 20
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class ParserFile(StrictModel):
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(min_length=1, max_length=4 * ((MAX_BYTES + 2) // 3))


class Candidate(StrictModel):
    document_type: str
    description: str = Field(min_length=1, max_length=2000)


class ClassifyRequest(StrictModel):
    files: list[ParserFile] = Field(min_length=1, max_length=1)
    document_types: list[Candidate] = Field(min_length=1, max_length=100)


class ExtractRequest(StrictModel):
    files: list[ParserFile] = Field(min_length=1, max_length=1)
    document_type: str
    schema_version: int = Field(ge=1)
    schema_hash: Digest
    canonical_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


class SourcePage(StrictModel):
    source_file: str
    page: int = Field(ge=1, le=MAX_PAGES)
    source_sha256: Digest
    page_sha256: Digest


class ClassifiedPage(SourcePage):
    document_type: str | None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=2000)
    model: str
    cache_hit: bool


class ClassificationResponse(StrictModel):
    pages: list[ClassifiedPage] = Field(min_length=1, max_length=MAX_PAGES)


class SchemaIssue(StrictModel):
    path: list[str | int]
    keyword: str


class ExtractionResponse(StrictModel):
    document_type: str
    schema_version: int = Field(ge=1)
    schema_hash: Digest
    status: Literal["draft"]
    attributes: dict[str, JsonValue]
    canonical_schema_valid: bool
    canonical_schema_errors: list[SchemaIssue]
    pages: list[SourcePage] = Field(min_length=1, max_length=MAX_PAGES)
    model: str
    cache_hit: bool
