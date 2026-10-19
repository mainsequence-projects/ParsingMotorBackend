# Document Extraction API

The parser classifies pages and extracts document attributes against caller-supplied
schemas. Back Office owns document sets, completeness requirements, cross-document
checks, reviewer decisions, recommended next steps, client requests, audit history,
and signed HTML/PDF protocols. Loans and new accounts use that same Back Office engine.

## Supported routes

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Version, region and ingestion limits |
| GET | `/session` | Existing short-lived Lambda invocation credentials |
| POST | `/classify_json` | Caller-catalog page classification |
| POST | `/extract_typed_json` | Explicit-type extraction with canonical/draft schemas |

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
validators and business checks before final submission. The response lists source pages without field-level citations or Textract corroboration.

These two endpoints do not create legacy `/runs` audit records or persist uploads. The
calling Back Office workflow owns storage, associating the result to the person/document
set, and review. The legacy loan endpoints have been removed.

## Validation and limits

Requests use strict Pydantic contracts. Validation errors omit submitted inputs.
Provider responses must satisfy the supplied draft schema; `canonical_schema_valid`
reports structural completeness only. It never means approved or accepted.

Default ingestion limits are 12 files, 20 total pages and 15 MiB per decoded file. Pages are never silently truncated. Invalid
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
