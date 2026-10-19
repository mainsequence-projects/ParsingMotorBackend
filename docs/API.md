# Document Extraction API

The parser classifies pages, extracts document attributes against caller-supplied
schemas, and compares supplied cited facts. Back Office owns document sets, completeness requirements, cross-document
checks, reviewer decisions, recommended next steps, client requests, audit history,
and signed HTML/PDF protocols. Loans and new accounts use that same Back Office engine.

## Supported routes

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Version, region and ingestion limits |
| GET | `/session` | Existing short-lived Lambda invocation credentials |
| POST | `/classify_json` | Caller-catalog page classification |
| POST | `/extract_typed_json` | Explicit-type extraction with canonical/draft schemas |
| POST | `/compare_documents_json` | Cited observations over supplied extracted facts |

The root redirects to `/docs`. Protected routes require `x-api-key` when configured;
Lambda invocation also retains its existing SigV4 authentication. Tokens in query
parameters are not accepted. `GET /session` retains the configured invocation role and
Function URL contract so existing Back Office server clients can continue to call it.

**Breaking removal:** `/extract`, `/extract_json`, `/runs`, `/runs/*`, `/rules`, `/rules/proposals/*`, `/learning`, and `/eval/summary`
now return 404. Their CLI equivalents (`recheck`, `decide`, `verify`, `learn`, and
`protocol`) and the loan demo are removed. There is no compatibility route that runs
business rules inside this service. Historical samples and project notes are reference
material, not the current API contract. This source change does not deploy a service.

## `POST /classify_json` — discovery only

Accepts a strict `ClassifyJsonRequest`. The caller supplies the current canonical type
catalog; there is no built-in loan taxonomy. Types must be unique.

```json
{
  "files": [{"filename": "document.pdf", "content_base64": "<standard base64>"}],
  "document_types": [
    {"document_type": "passport", "description": "Travel document identifying its holder"},
    {"document_type": "bank_statement", "description": "Bank account statement with balances"}
  ]
}
```

Returns `{"pages": [...]}` in file/page order. Each entry has `source_file`, one-based
`page`, `source_sha256`, `page_sha256`, `document_type`, `confidence` (0–1), `reason`,
`model`, and `cache_hit`. `document_type: null` means unrecognized/uncertain. Classification
is per page: the caller selects/groups the pages belonging to a logical document before
requesting extraction. No attributes are extracted and no loan rules are evaluated.

## `POST /extract_typed_json` — explicit type and full canonical contract

Accepts `ExtractTypedJsonRequest`. Send **one logical document**, possibly spanning several
files, with its explicit type. This endpoint skips classification. It uses Bedrock vision
with all supplied pages and the entire JSON Schemas; the legacy flat Textract queries and
loan rules are not used. The operation is synchronous.

Construct the request directly from the Back Office canonical models; do not flatten the
schemas to an attribute-name list or provide business policy as extraction configuration:

```python
from backoffice.schemas.catalog import document_schema

contract = document_schema("bank_statement", version=1)
request = {
    "files": [{"filename": "statement.pdf", "content_base64": encoded_file}],
    "document_type": contract.document_type,
    "schema_version": contract.version,
    "schema_hash": contract.content_sha256,
    "canonical_schema": contract.canonical_schema,
    "output_schema": contract.draft_schema,
}
# SigV4 POST {functionUrl}extract_typed_json with x-api-key.
```

Required fields: `files`, `document_type`, positive integer `schema_version`, lowercase
SHA-256 `schema_hash`, `canonical_schema`, and `output_schema`. Both schemas are complete
JSON Schema Draft 2020-12 objects, including descriptions, required fields, nested objects,
arrays, constraints, `$defs`, and local `$ref` references. Their root must have
`type: object`, `additionalProperties: false`, and `properties.document_type.const` equal
to the explicit type. The caller owns the type/version registry. The hash verifies the
submitted schema pair's integrity, not its authenticity or registration in Back Office.

The checksum is exactly the Back Office catalog checksum:

```python
sha256(json.dumps(
    {"canonical_schema": canonical_schema, "draft_schema": output_schema},
    sort_keys=True, separators=(",", ":"), ensure_ascii=False,
).encode("utf-8")).hexdigest()
```

Only local JSON Pointer references are supported; unresolved/external/recursive references,
`$id`, anchors, and dynamic references are rejected. Each schema is limited to 256 KiB,
4096 schema nodes, and 64 levels. A different declared dialect or an unsupported format is rejected; date, date-time, and UUID formats are validated. These limits
are independent of a particular business document catalog.

Response fields:

| Field | Meaning |
|---|---|
| `document_type`, `schema_version`, `schema_hash` | Exact supplied contract identity |
| `status` | Always `draft`; never automatic approval |
| `attributes` | Nested extracted object, validated against `output_schema` without coercion |
| `canonical_schema_valid` | Whether the draft also passes the complete JSON Schema |
| `canonical_schema_errors` | Failed JSON Schema keyword and instance path; no repeated field values |
| `pages` | Source file, physical page number, source and rendered-page SHA-256 hashes |
| `model`, `cache_hit` | Bedrock model and cache provenance |

Missing/unreadable values can be omitted or null only where `output_schema` allows it.
An upstream result violating that schema, or changing/omitting `document_type`, returns
502. No invalid result is returned as a successful extraction. `canonical_schema_valid`
only covers JSON Schema checks: Back Office must still apply canonical Pydantic Python
validators and business checks before final submission. The v1 response lists source pages without field-level citations. The v2 contract adds
field evidence as described below; neither version claims Textract corroboration unless
that reader actually supplied the evidence.

These two endpoints do not create legacy `/runs` audit records or persist uploads. The
calling Back Office workflow owns storage, associating the result to the person/document
set, and review. The legacy loan endpoints have been removed.

## Version 2 evidence

Include `contract_version: 2` in the same operations to use the richer wire contracts.
Classification adds printed pagination and grouping observations. Typed extraction adds
reader candidates, JSON Pointer field references, source locations, normalization version,
evidence coverage and provider usage. The default reader produces page-level citations;
unmeasured field confidence and bounding boxes remain null. Candidate agreement describes
readings of the same document; it is not a business check or acceptance decision.

Back Office persists and authorizes access to this evidence. The parser does not own
business persons, assignments, reviews or lending state. See the generated
[OpenAPI contract](openapi.json) for exact v1/v2 request and response schemas.

## Validation and limits

Requests use strict Pydantic contracts. Validation errors omit submitted inputs.
Provider responses must satisfy the supplied draft schema; `canonical_schema_valid`
reports structural completeness only. It never means approved or accepted.

Default ingestion limits are 12 files, 20 total pages and 15 MiB per decoded file;
v2 requests accept one original file. Pages are never silently truncated. Invalid
request contracts return 422, unsupported input content 415, page limits 413, provider
failures or invalid provider output 502, and budget exhaustion 503. Lambda's request-body
limit may be lower and includes base64 overhead and the supplied schemas.

## CLI and signed requests

```sh
python -m extractor classify classify-request.json -o classification.json
python -m extractor extract-typed extract-request.json -o extraction.json
python deploy/signed_extract.py classify_json classify-request.json --public-url https://example.invalid/
python deploy/signed_extract.py extract_typed_json extract-request.json --public-url https://example.invalid/
```

The JSON file is the full HTTP request, including the catalog for classification or
canonical/draft schema pair for extraction. No implicit loan configuration is loaded.

## Version 2 evidence contracts

Both document endpoints additionally accept `contract_version: 2`. Omitting it keeps
the existing v1 contract. V2 permits exactly one source file (up to 20 pages); invalid
versions fail validation rather than selecting another contract. Request schemas and
hashes undergo the same validation as v1 before any provider call.

Classification additionally returns observed printed pagination, boundary and group
hints, the actual applied rotation, and usage. These hints do not decide document
membership. Source and rendered-page hashes come from ingestion, never model output.

Typed extraction additionally accepts `reader_mode: "single" | "independent_readers"`
and optional `page_selection: [3, 1]`. Selection retains the original physical numbers
and requested order; missing or duplicate pages are rejected. The response contains
`normalization_version`, `field_evidence`, `evidence_complete`, and usage. Each field
uses an RFC 6901 pointer into attributes, with actual reader/model identities, values,
source transcription when available, and citations to supplied pages. Every non-null
leaf except `document_type` must be cited. Invalid or omitted citations cause a
sanitized 502; they are never invented to satisfy the contract.

The current reader uses schema-guided vision and page-level citations. Field confidence
and boxes are null because this reader does not measure them. `independent_readers`
performs two separate vision reads using the extraction model and classification model;
the second sees the source and schema, never the first reader's result. The actual models
must differ. This is not independent OCR, and correlated model errors remain possible.
Different field/array structures fail closed pending explicit alignment support.

The primary reading supplies draft attributes. Exact candidate agreement is reported
without tolerances, review statuses or automatic resolution. Back Office decides what
disagreement means. Classification scores, citation coverage and structural schema
validity are not field accuracy or acceptance decisions. No v2 output adds a loan,
account or other business workflow.

The checked-in `tests/fixtures/processing_v2` schemas mirror Back Office's exported wire
contracts. Parity tests cover both requests and responses; update the snapshots when
coordinating a contract change. The runtime does not import Back Office code.

## `POST /compare_documents_json` — cited observations across documents

Send `contract_version: 1`, `documents`, and `comparisons`. This is the comparison
operation's first contract; classification/extraction v1/v2 are unchanged. Each document
has an opaque `document_id`, exact `revision`, type, schema version/hash, evidence hash
and `facts`. A fact has a JSON Pointer, value and source citations (source ID/checksum,
physical page/page checksum, optional box and recorded rotation). Each comparison has
a unique key, factual topic, left/right fact references and optional context references.
The operation accepts arbitrary document types; it contains no loan/account taxonomy.

```json
{
  "key": "income-pair-1",
  "topic": "Monthly net income, reporting period and basis",
  "left": {"document_id": "document-a", "pointer": "/monthly_income/amount"},
  "right": {"document_id": "document-b", "pointer": "/net_pay/amount"},
  "context": [
    {"document_id": "document-a", "pointer": "/monthly_income/currency"},
    {"document_id": "document-b", "pointer": "/net_pay/currency"}
  ]
}
```

This example is one entry in `comparisons`; all references must resolve to supplied
cited facts. The complete models and examples are covered by the OpenAPI contract and
`tests/test_document_comparison.py`. Bounds are 2–20 documents, up to 100 facts each,
1–50 comparison pairs, and 512 KiB normalized request JSON. Duplicate IDs/keys,
unknown references, inconsistent source hashes, invalid boxes and policy fields are
rejected before the provider call. The existing `x-api-key` dependency applies.

The model returns only comparability (`comparable`, `not_comparable`, `uncertain`),
relationship (`equivalent`, `different`, `uncertain`) and a factual explanation for
each key. Code attaches the exact values, document revisions, source citations and
context from the request and computes `request_sha256`. No original-file fetch occurs.
An incomplete or invalid model response produces a sanitized 502.

Amounts with different currencies or unclear periods/bases must not be assumed
comparable. There is no tolerance, conversion, severity, requirement status, approval,
preferred value or recommended next step. Back Office owns all such rules and results,
using these observations in its shared engine for every document-set policy.
