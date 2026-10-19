"""Generic cited comparison observations; no business rules cross this boundary."""

import hashlib
import json
import re
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .evidence_base import Digest, StrictModel
from .evidence_contracts import ProviderUsage

Identifier = Annotated[str, Field(min_length=1, max_length=120)]
Pointer = Annotated[str, Field(min_length=1, max_length=500)]


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class ComparisonCitation(StrictModel):
    source_id: Identifier
    source_sha256: Digest
    page: int = Field(ge=1, le=20)
    page_sha256: Digest
    bounding_box: list[float] | None = Field(default=None, min_length=4, max_length=4)
    rotation_degrees: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def valid_box(self) -> Self:
        if self.bounding_box is not None:
            left, top, right, bottom = self.bounding_box
            if not 0 <= left < right <= 1 or not 0 <= top < bottom <= 1:
                raise ValueError("invalid normalized citation box")
        return self


class ComparisonFact(StrictModel):
    pointer: Pointer
    value: JsonValue
    citations: list[ComparisonCitation] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_fact(self) -> Self:
        if not self.pointer.startswith("/") or re.search(r"~(?![01])", self.pointer):
            raise ValueError("invalid JSON Pointer")
        if self.value is None:
            raise ValueError("comparison facts require an observed value")
        return self


class ComparisonDocument(StrictModel):
    document_id: Identifier
    revision: int = Field(ge=1)
    document_type: Identifier
    schema_version: int = Field(ge=1)
    schema_hash: Digest
    evidence_sha256: Digest
    facts: list[ComparisonFact] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_facts(self) -> Self:
        if len({f.pointer for f in self.facts}) != len(self.facts):
            raise ValueError("duplicate fact pointer")
        return self


class FactReference(StrictModel):
    document_id: Identifier
    pointer: Pointer


class ComparisonQuestion(StrictModel):
    key: Identifier
    topic: str = Field(min_length=1, max_length=500)
    left: FactReference
    right: FactReference
    context: list[FactReference] = Field(default_factory=list, max_length=20)


class CompareDocumentsRequest(StrictModel):
    contract_version: Literal[1]
    documents: list[ComparisonDocument] = Field(min_length=2, max_length=20)
    comparisons: list[ComparisonQuestion] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        documents = {d.document_id: d for d in self.documents}
        if len(documents) != len(self.documents):
            raise ValueError("duplicate document identity")
        if len({q.key for q in self.comparisons}) != len(self.comparisons):
            raise ValueError("duplicate comparison key")
        used = set()
        sources, pages = {}, {}
        for document in self.documents:
            for fact in document.facts:
                for citation in fact.citations:
                    if (
                        sources.setdefault(citation.source_id, citation.source_sha256)
                        != citation.source_sha256
                    ):
                        raise ValueError("conflicting source checksum")
                    key = (citation.source_id, citation.page)
                    identity = (citation.page_sha256, citation.rotation_degrees)
                    if pages.setdefault(key, identity) != identity:
                        raise ValueError("conflicting page identity")
        for question in self.comparisons:
            if question.left.document_id == question.right.document_id:
                raise ValueError("comparison requires two distinct documents")
            for ref in [question.left, question.right, *question.context]:
                fact_for(self, ref)
                used.add(ref.document_id)
        if set(documents) != used:
            raise ValueError("unreferenced comparison document")
        if len(canonical_json(self.model_dump(mode="json")).encode()) > 512 * 1024:
            raise ValueError("comparison evidence exceeds 512 KiB")
        return self

    def checksum(self):
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


def fact_for(request, ref):
    for document in request.documents:
        if document.document_id == ref.document_id:
            for fact in document.facts:
                if fact.pointer == ref.pointer:
                    return document, fact
    raise ValueError("reference is outside supplied evidence")


class CitedFact(ComparisonFact):
    document_id: Identifier
    revision: int = Field(ge=1)


def cited_fact(request, ref):
    document, fact = fact_for(request, ref)
    return CitedFact(
        document_id=document.document_id, revision=document.revision, **fact.model_dump()
    )


class ComparisonObservation(StrictModel):
    key: Identifier
    comparability: Literal["comparable", "not_comparable", "uncertain"]
    relationship: Literal["equivalent", "different", "uncertain"]
    explanation: str = Field(min_length=1, max_length=2000)
    left: CitedFact
    right: CitedFact
    context: list[CitedFact] = Field(default_factory=list, max_length=20)


class CompareDocumentsResponse(StrictModel):
    contract_version: Literal[1]
    request_sha256: Digest
    observations: list[ComparisonObservation] = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    cache_hit: bool
    usage: list[ProviderUsage] = Field(default_factory=list, max_length=10)

    def validate_request(self, request):
        if self.request_sha256 != request.checksum():
            raise ValueError("comparison response belongs to different inputs")
        expected = {q.key: q for q in request.comparisons}
        if len(self.observations) != len(expected) or {o.key for o in self.observations} != set(
            expected
        ):
            raise ValueError("comparison response must cover every requested pair exactly once")
        for observation in self.observations:
            question = expected[observation.key]
            for side in ("left", "right"):
                if canonical_json(getattr(observation, side).model_dump()) != canonical_json(
                    cited_fact(request, getattr(question, side)).model_dump()
                ):
                    raise ValueError("comparison changed a cited fact")
            if canonical_json([c.model_dump() for c in observation.context]) != canonical_json(
                [cited_fact(request, ref).model_dump() for ref in question.context]
            ):
                raise ValueError("comparison changed context evidence")
        return self


COMPARISON_CONTRACTS = (CompareDocumentsRequest, CompareDocumentsResponse)
