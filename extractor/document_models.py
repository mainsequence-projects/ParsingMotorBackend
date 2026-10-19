"""Strict request/response contracts for schema-driven parsing operations."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from .ingest import MAX_FILE_MB, MAX_FILES
from .schema_contract import schema_hash, validate_schema

DocumentTypeId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
MAX_BYTES = int(MAX_FILE_MB * 1024 * 1024)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FileB64(StrictModel):
    filename: str = Field("upload.bin", min_length=1, max_length=300)
    content_base64: str = Field(min_length=1, max_length=4 * ((MAX_BYTES + 2) // 3))

    @field_validator("content_base64")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("content_base64 must be valid standard base64") from exc
        if not decoded or len(decoded) > MAX_BYTES:
            raise ValueError("decoded file must be nonempty and within the configured file limit")
        return value


class UploadRequest(StrictModel):
    files: list[FileB64] = Field(min_length=1, max_length=MAX_FILES)

    def decoded_files(self) -> list[tuple[str, bytes]]:
        return [(f.filename, base64.b64decode(f.content_base64, validate=True)) for f in self.files]


class ExtractJsonRequest(UploadRequest):
    explain: bool = False


class DocumentTypeCandidate(StrictModel):
    document_type: DocumentTypeId
    description: str = Field(min_length=1, max_length=2000)


class ClassifyJsonRequest(UploadRequest):
    document_types: list[DocumentTypeCandidate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_types(self):
        names = [candidate.document_type for candidate in self.document_types]
        if len(names) != len(set(names)):
            raise ValueError("document_types must have unique document_type values")
        return self


class ExtractTypedJsonRequest(UploadRequest):
    document_type: DocumentTypeId
    schema_version: int = Field(ge=1)
    schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]

    @model_validator(mode="after")
    def valid_contract(self):
        for schema in (self.canonical_schema, self.output_schema):
            validate_schema(schema, self.document_type)
        if self.schema_hash != schema_hash(self.canonical_schema, self.output_schema):
            raise ValueError("schema_hash does not match the canonical/draft schema pair")
        return self


class SourcePage(StrictModel):
    source_file: str
    page: int = Field(ge=1)
    source_sha256: str
    page_sha256: str


class Classification(StrictModel):
    document_type: DocumentTypeId | None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=2000)


class ClassifiedPage(SourcePage, Classification):
    model: str
    cache_hit: bool


class ClassifyJsonResponse(StrictModel):
    pages: list[ClassifiedPage]


class SchemaIssue(StrictModel):
    path: list[str | int]
    keyword: str


class ExtractTypedJsonResponse(StrictModel):
    document_type: DocumentTypeId
    schema_version: int
    schema_hash: str
    status: Literal["draft"] = "draft"
    attributes: dict[str, JsonValue]
    canonical_schema_valid: bool
    canonical_schema_errors: list[SchemaIssue]
    pages: list[SourcePage]
    model: str
    cache_hit: bool
