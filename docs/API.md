# KB Document Control Extractor — API v0.4.0

> ES: Contrato HTTP para Jose (frontend + Main Sequence). La fuente de verdad formal es
> [`docs/openapi.json`](openapi.json) (generado con `python scripts/export_openapi.py`; un
> test falla si queda desactualizado). Todas las respuestas se validan con los modelos de
> `extractor/api_models.py`: se pueden **añadir** claves en el futuro, nunca quitar.
>
> EN: HTTP contract for Jose (frontend + Main Sequence). The formal source of truth is
> [`docs/openapi.json`](openapi.json). Every response is validated against the models in
> `extractor/api_models.py` and `extractor/document_models.py`: keys may be **added** later, never removed.

| | |
|---|---|
| Región / Region | `eu-central-1` (Frankfurt) |
| Demo (CloudFront) | `https://d31su4idgeitei.cloudfront.net/` |
| Lambda Function URL (AWS_IAM, SigV4) | `https://mokpk7khaf3azp3fhb3h4ruiqm0gflxj.lambda-url.eu-central-1.on.aws/` |
| Local | `uvicorn extractor.api:app --port 8000` → `http://localhost:8000/docs` (Swagger) |

---

## 1. Autenticación / Authentication

**ES.** Dos capas:

1. **Token de demo** en la cabecera `x-api-key` (solo cabecera; en la query string se
   ignora porque acabaría en logs). Si el servidor tiene `EXTRACTOR_API_KEY` vacío (local),
   no se pide. `/health` nunca lo pide. El token se entrega por canal privado, no está en
   el repo (`.deploy_token`, gitignored).
2. **Acceso a la Lambda.** La Function URL usa `AuthType AWS_IAM` (la organización prohíbe
   `NONE`). Dos caminos:
   - **GET por CloudFront** (`/health`, `/rules`, `/runs/...`, `/learning`, `/`): CloudFront
     firma hacia la Lambda con OAC; el cliente solo manda `x-api-key`.
   - **POST de extracción**: OAC no puede firmar cuerpos de POST, así que el navegador pide
     `GET /session` (con `x-api-key`) y recibe credenciales STS de **15 min** que solo pueden
     invocar esta función; con ellas firma SigV4 (`service=lambda`) un
     `POST {functionUrl}extract_json`. Es lo que hace la página demo (`/`).
   - **Main Sequence / backend a backend**: firmar SigV4 con su propio rol IAM con permiso
     `lambda:InvokeFunctionUrl` (o usar `/session` igual que el navegador), y mandar también
     `x-api-key`. Ejemplo en `deploy/signed_extract.py`.

**EN.** Two layers: (1) demo token in the `x-api-key` header (header only; not required when
`EXTRACTOR_API_KEY` is unset locally; never on `/health`). (2) The Function URL is
`AWS_IAM`. GETs go through CloudFront (OAC signs for you). POST bodies cannot be signed by
OAC, so call `GET /session` → 15-minute STS creds scoped to this function → SigV4-sign
(`service=lambda`, region `eu-central-1`) a `POST {functionUrl}extract_json`. Server-to-server
callers can sign with their own IAM role (`lambda:InvokeFunctionUrl`) and still send
`x-api-key`. See `deploy/signed_extract.py`.

**CORS.** ES: en Lambda lo pone la Function URL (`*`, GET/POST). En local/Main Sequence se
activa con `EXTRACTOR_CORS_ORIGINS="http://localhost:5173,https://..."`; apagado por
defecto. **No** activarlo en Lambda (cabeceras duplicadas → el navegador rechaza).
EN: on Lambda the Function URL adds CORS. Locally set `EXTRACTOR_CORS_ORIGINS` (comma list);
off by default; never on Lambda (duplicated headers break browsers).

---

## 2. Endpoints

| Método | Ruta | Auth | ES | EN |
|---|---|---|---|---|
| GET | `/health` | — | Estado, versión, commit, versión de reglas, límites | Status, version, commit, rules version, limits |
| GET | `/session` | key | Credenciales STS 15 min para SigV4 | 15-min STS creds for SigV4 |
| POST | `/extract` | key (+SigV4 en Lambda) | multipart `files` (1..12) | multipart `files` |
| POST | `/extract_json` | key (+SigV4 en Lambda) | JSON con archivos en base64 | JSON with base64 files |
| POST | `/classify_json` | key (+SigV4 en Lambda) | Solo clasificación | Discovery only, supplied type catalog |
| POST | `/extract_typed_json` | key (+SigV4 en Lambda) | Tipo explícito + esquemas completos | Explicit type + full canonical/draft schemas |
| GET | `/runs/{run_id}` | key | Estado actual (motor + decisiones humanas) | Current state |
| POST | `/runs/{run_id}/decisions` | key | Añade decisión humana y recalcula | Append human decision, re-evaluate |
| GET | `/runs/{run_id}/verify` | key | Verifica la cadena de hashes | Verify hash chain |
| GET | `/runs/{run_id}/protocol?format=html\|pdf` | key | Protocolo de control citado (HTML/PDF) | Cited control protocol |
| GET | `/learning?min_support=3` | key | Overrides humanos por regla (sugerencias, nunca auto) | Human overrides per rule |
| GET | `/rules` | key | rules.yaml activo + versión | Active rules.yaml + version |
| GET | `/rules/proposals?min_support=3` | key | Cambios concretos de rules.yaml aprendidos de decisiones + impacto | Learned rules.yaml changes + impact |
| POST | `/rules/proposals/{proposal_id}/review` | key | Aprobar/rechazar propuesta → nueva versión (no se activa sola) | Approve/reject → new version (not auto-activated) |
| GET | `/` | — | Página demo HTML | HTML demo |

`run_id` = `^\d{8}T\d{6}Z-[0-9a-f]{8}$` (ej. `20261019T125642Z-161958ed`).

### 2.1 `GET /health` → `HealthResponse`
```json
{"status":"ok","version":"0.3.0","region":"eu-central-1","git_commit":"5cb6672",
 "rules_version":"3f1c...16hex","rules_error":null,"audit_storage":"ephemeral",
 "limits":{"max_files":12,"max_pages":20,"max_file_mb":15}}
```
ES: `audit_storage: ephemeral` = Lambda `/tmp`; un run puede no existir tras un cold start o
en otro contenedor (→ 404). EN: `ephemeral` means runs may vanish across containers.

### 2.2 `POST /extract` y `POST /extract_json` → `ExtractResponse`
Entrada / input:
- `/extract`: `multipart/form-data`, campo `files` repetible; `?explain=true` añade una
  explicación LLM en campos en desacuerdo (cuesta más). 
- `/extract_json`: `{"files":[{"filename":"package.pdf","content_base64":"..."}],"explain":false}`

Tipos aceptados por contenido (magic bytes, no por extensión): PDF, PNG, JPEG (TIFF/WebP si
Pillow los abre). Límites (env `EXTRACTOR_MAX_FILES/PAGES/FILE_MB`): 12 archivos, 20 páginas
en total, 15 MB por archivo, 50 MP por imagen. **En Lambda el cuerpo total ≤ 6 MB** (base64
crece ×1.33 ⇒ ~4.4 MB de PDF).

Salida (resumen; ver openapi para todo):

| Clave | ES | EN |
|---|---|---|
| `run_id` | id del run y de su audit log | run / audit id |
| `documents[]` | documentos lógicos: `doc_id`, `doc_type`, `pages[]` (archivo+página citados), `fields{}`, `checks[]` | logical documents |
| `documents[].fields.X` | `value`, `normalized`, `textract`/`textract_conf`/`textract_page`, `bedrock`/`bedrock_page`, `source` (`both`/`textract`/`bedrock`), `confidence` (null = solo LLM), `page`, `bbox`, `outcome`, `status`, `rule_ids` | per-field result with both engines and the cited page |
| `checks[]` | checks del paquete (cruce de documentos, completitud): `check_id`, `rule_id`, `status`, `detail`, `evidence[]` | package checks |
| `required_documents`, `required_documents_source` | lista requerida (del contrato o por defecto) | required list |
| `missing_documents`, `missing_pages`, `unexpected_documents` | faltantes / páginas faltantes / sobrantes | completeness |
| `combined_confidence`, `fields_auto_validated_pct` | métricas | metrics |
| `recommended_next_step` | ver §3 | see §3 |
| `draft_client_message` | borrador de email al cliente (inglés, compatibilidad) o `null` | client email draft (EN) or null |
| `draft_client_messages` | `{"cs": "...", "en": "..."}` el mismo borrador en checo (el que se envía) e inglés, o `null` | same draft in Czech + English |
| `models`, `rules_version`, `engine_version`, `git_commit`, `inputs[]` (sha256) | trazabilidad | provenance |
| `cost` | coste del run y caché | run cost |

`field.status`: `passed` · `warning` · `manual_review` · `missing`.
`check.status`: `passed` · `warning` · `discrepancy` · `manual_review`.
`field.outcome` (ejemplos): `agree_high_conf`, `agree_exact`, `agree_low_conf`,
`agree_ocr_no_diacritics` (Textract no lee checo y pierde ř/č/š/í…; si la única diferencia
son esas letras cuenta como acuerdo y se guarda la grafía del LLM), `disagree`,
`ocr_unparseable`, `llm_only`, `textract_only`, `both_missing`.

**Tipos de documento (`doc_type`)** v0.4: `contract`, `id_card` (občanský průkaz),
`bank_statement` (výpis z účtu), `payslip` (výplatní lístek), `proof_of_address`,
`employer_confirmation` (potvrzení o zaměstnání a výši příjmu), `tax_or_debt_certificate`
(potvrzení o bezdlužnosti, Finanční úřad), `other`. El contrato define cuáles (5–7) son
obligatorios; ver `GET /rules` y `extractor/schemas.yaml` para los campos.

### 2.2a `POST /classify_json` — discovery only

Accepts a strict `ClassifyJsonRequest`. The caller supplies the current canonical type
catalog; the legacy Czech-loan taxonomy is not used. Types must be unique.

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

### 2.2b `POST /extract_typed_json` — explicit type and full canonical contract

Accepts `ExtractTypedJsonRequest`. Send **one logical document**, possibly spanning several
files, with its explicit type. This endpoint skips classification. It uses Bedrock vision
with all supplied pages and the entire JSON Schemas; the legacy flat Textract queries and
loan rules are not used. The operation is synchronous.

Construct the request directly from the Back Office canonical models; do not flatten the
schemas to an attribute-name list or copy the parser's legacy `schemas.yaml`:

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
accept all ten current canonical Back Office models (version 1).

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
validators and business checks before final submission. The response lists source pages;
it does not claim field-level citations or Textract corroboration.

These two endpoints do not create legacy `/runs` audit records or persist uploads. The
calling Back Office workflow owns storage, associating the result to the person/document
set, and review. Existing `/extract` and `/extract_json` retain the legacy audited workflow.

### Strict JSON requests and limits

All three JSON upload routes use strict Pydantic validation: unknown envelope/file fields,
wrong scalar types (including `"false"` for a boolean or `"1"` for a version), invalid base64,
empty uploads, and invalid schema contracts return **422** before any provider call.
Validation errors omit the input so uploaded content is not echoed. This intentionally
changes `/extract_json` malformed-envelope responses from 400 to 422 and stops silently
ignoring unsupported options. Its valid `files`/`explain` request shape is unchanged.

The same API-key and Lambda SigV4 requirements apply to both new routes. Existing ingestion
limits apply (default 12 files, 20 pages total, 15 MiB per decoded file); typed extraction
also caps pages at 20 if the configured ingestion limit is raised. Pages are never silently
truncated. Schema/file-list limits are 422; ingestion page limits are 413; unsupported
content is 415; corrupt documents are 422. Budget exhaustion is 503 and provider/output
validation failures are 502. Lambda's total request-body limit can be lower than these
application limits and includes both base64 files and schemas.

### 2.3 `POST /runs/{run_id}/decisions` → `DecisionResponse`
```json
{"field":"contract.declared_monthly_income","action":"edit","new_value":"38000",
 "reviewer":"jose","comment":"client confirmed"}
```
| action | target | ES efecto | EN effect |
|---|---|---|---|
| `confirm` | `field` o `check` | campo → `passed`; check: sin cambio (de acuerdo) | field passed; check unchanged |
| `edit` | `field` | nuevo valor (debe normalizar al tipo) → `passed` | new value, must parse |
| `edit` | `check` | `new_value` = status (`passed`/`warning`/`discrepancy`/`manual_review`) | set status |
| `reject` | `field` | valor descartado → `missing` | value discarded |
| `reject` | `check` | el hallazgo del motor es falso → se invierte | flip the finding |
| `request_documents` | — | `new_value`: lista de doc types → aparecen como faltantes | ask client for docs |

Exactamente uno de `field` (`doc_type.campo` o `doc_id.campo`) o `check` (`check_id`,
`rule_id` o nombre). Límites: reviewer 100, comment 2000, new_value 500 caracteres.
Respuesta: `next_step_before`, `next_step`, `decision_event`, `reevaluation_event` (eventos
del audit con `seq`, `hash`, `prev_hash`). Nunca se modifica un evento anterior.

### 2.4 `GET /runs/{run_id}` → `RunState`
Igual que la evaluación de `/extract` aplicando las decisiones, más `decisions[]`. Campos o
checks tocados por un humano llevan `engine` (lo que dijo el motor) y `human`.

### 2.5 `GET /runs/{run_id}/verify` → `VerifyResponse`
`{"ok":true,"file":"<run>.jsonl","run_id":"...","events":7,"head_hash":"...","errors":[]}`.
`ok:false` si alguien editó/borró/reordenó una línea.

### 2.6 `GET /learning` → `LearningResponse`, `GET /rules` → `RulesResponse`
Learning agrega decisiones por `rule_id` y sugiere ajustes (`auto_applied` siempre `false`).
Rules devuelve el YAML activo y su versión (sha256[:16]).

### 2.6b Ciclo de mejora de reglas / Rule-learning loop
`GET /rules/proposals` → `RuleProposalsResponse`: cada propuesta cambia **un solo valor** de
rules.yaml (`rule_id`, `path` p. ej. `severity.out_of_tolerance`, `old` → `new`), con `rationale`,
`support` (nº de decisiones) e `impact`: todos los runs auditados re-evaluados offline (sin AWS)
con reglas actuales vs candidatas — `status_changes`, `next_step_changes` y
**`less_cautious_runs`** (runs que pasarían a un paso menos prudente: mirar antes de aprobar).
`history` = últimos eventos del ledger encadenado `rules_ledger.jsonl`.

`POST /rules/proposals/{proposal_id}/review` body `{"action":"approve|reject","reviewer":"…","comment":"…"}`
→ `RuleReviewResponse`. Approve guarda `rules/rules-<version>.yaml` en el audit dir y devuelve el
`yaml`; **no lo activa**: hay que poner `EXTRACTOR_RULES` o commitearlo como `extractor/rules.yaml`
y redesplegar. Propuesta desconocida, ya revisada o de otra versión base → 409. Reject: la
propuesta no se vuelve a ofrecer. En Lambda el audit es por contenedor: solo ve los runs de ese
contenedor.
EN: each proposal is a one-value patch with offline impact; approving writes a new version that a
human must activate. Nothing is applied automatically.

### 2.7 `GET /runs/{run_id}/protocol?format=html|pdf` → HTML o PDF
Protocolo de control citado para firmar/archivar, reconstruido solo desde el audit log
(CLI: `python -m extractor protocol <run_id> -o protocolo.pdf`). Contiene: cabecera (caso,
run_id, hora, versión motor/git, reglas, modelos, región, sha256 de entradas, estado de la
cadena de hashes), resumen (conteos y siguiente paso), tabla de todos los checks (rule_id,
descripción, estado, evidencia: doc, campo, página, valor Textract + confianza, valor
Bedrock, valor final), campos citados por documento, faltantes y borrador al cliente
(checo + inglés), historial de decisiones humanas y pie *"AI output supports the decision;
not an automatic final approval"*. `format` distinto de html/pdf → 422; run desconocido → 404.

---

## 3. `recommended_next_step`
Orden de prioridad (rules.yaml → `next_step`), gana la primera condición que se cumple:

| # | Valor | Cuándo (ES) | When (EN) |
|---|---|---|---|
| 1 | `manual_review_discrepancy` | algún check en `discrepancy` (ingresos ≠, RČ inválido, nombre ≠, deuda fiscal, certificado de otra persona…) | a cross-check discrepancy |
| 2 | `request_missing_documents` | falta un documento requerido o páginas | required doc/pages missing |
| 3 | `manual_review_extraction` | ≥ 3 campos en revisión manual | ≥ 3 fields in manual review |
| 4 | `manual_review_extraction` | algún campo sin valor (`missing`) | any field missing |
| 5 | `spot_check_warnings` | cualquier aviso o revisión manual restante | any warning / manual review |
| 6 | `approve_ready` | todo `passed` | all passed |

Una discrepancia gana a "pedir documentos": primero lo decide una persona y no se pide nada al
cliente antes. `draft_client_message` se rellena cuando hay algo que pedir al cliente.

---

## 4. Errores / Errors
Cuerpo siempre `{"detail": "..."}` (en 422 de validación de FastAPI, `detail` es una lista).

| Código | ES | EN |
|---|---|---|
| 400 | archivo vacío, cuerpo JSON mal formado, base64 inválido, sin archivos | empty file / bad JSON / bad base64 |
| 401 | falta o es incorrecto `x-api-key` | missing/invalid key |
| 403 | (Function URL) petición sin SigV4 válido — lo devuelve AWS, no la app | unsigned call to Function URL |
| 404 | run desconocido o no existe en este contenedor | unknown run |
| 409 | run incompleto/fallido o cadena de audit rota: no se aceptan decisiones | run incomplete / chain broken |
| 413 | demasiados archivos/páginas/MB, imagen > 50 MP (en Lambda también cuerpo > 6 MB, lo corta AWS) | too large |
| 415 | tipo de archivo no soportado | unsupported type |
| 422 | PDF corrupto o cifrado, 0 páginas, decisión inválida, parámetros fuera de rango | unreadable / invalid input |
| 500 | rules.yaml inválido o `/session` sin configurar | server misconfiguration |
| 502 | fallo de Textract/Bedrock/STS (detalle solo en CloudWatch) | upstream AWS failure |
| 503 | guardia de presupuesto (`EXTRACTOR_BUDGET_USD`) alcanzada | budget guard |
| 504 | (Lambda) más de 180 s — lo devuelve AWS | Lambda timeout |

---

## 5. curl

```bash
# local (sin token si EXTRACTOR_API_KEY no está definido)
curl -s localhost:8000/health
curl -s -F files=@samples/03_amount_mismatch/package.pdf localhost:8000/extract > out.json
RUN=$(python -c "import json;print(json.load(open('out.json'))['run_id'])")
curl -s -H 'content-type: application/json' localhost:8000/runs/$RUN/decisions \
  -d '{"field":"contract.declared_monthly_income","action":"edit","new_value":"38000","reviewer":"jose"}'
curl -s localhost:8000/runs/$RUN/verify

# desplegado (GET por CloudFront)
CF=https://d31su4idgeitei.cloudfront.net
curl -s $CF/health
curl -s -H "x-api-key: $KB_TOKEN" $CF/rules | head -c 200
curl -s -o /dev/null -w '%{http_code}\n' $CF/session          # 401 sin token

# desplegado (POST firmado SigV4 con credenciales de /session)
python deploy/signed_extract.py samples/01_clean/package.pdf
# alternativa con curl >= 7.75 (NO probada en la revisión; la referencia es signed_extract.py):
FURL=https://mokpk7khaf3azp3fhb3h4ruiqm0gflxj.lambda-url.eu-central-1.on.aws
B64=$(base64 -w0 samples/01_clean/package.pdf)
printf '{"files":[{"filename":"01_clean.pdf","content_base64":"%s"}]}' "$B64" > body.json
curl -s --aws-sigv4 "aws:amz:eu-central-1:lambda" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "x-amz-security-token: $AWS_SESSION_TOKEN" -H "x-api-key: $KB_TOKEN" \
  -H 'content-type: application/json' --data-binary @body.json $FURL/extract_json
```

---

## 6. Límites conocidos / Known limits
- ES: audit en Lambda en `/tmp` (efímero y por contenedor): un `run_id` (y su protocolo) puede
  dar 404 en otra invocación. Hacerlo durable exige un recurso nuevo (bucket S3 o tabla
  DynamoDB): pendiente de aprobación de Carlos. EN: Lambda audit is ephemeral/per container;
  durable storage needs a new resource (S3/DynamoDB), pending approval.
- Guardia de presupuesto en Lambda es por contenedor (no global).
- Solo las primeras 6 páginas de un documento van a Bedrock (`MAX_BEDROCK_PAGES`).
- Dos documentos del mismo tipo sin clave distintiva se fusionan; los checks cruzados usan el
  primero de cada tipo.
- Textract no tiene modelo checo: pierde letras acentuadas y no entiende prosa checa (cartas).
  Se compensa en reglas deterministas (acuerdo sin diacríticos, meses OCR, regla OCR para
  nedoplatek), pero fechas en cartas en prosa quedan a menudo en revisión manual.
- Perfiles `eu.*` de Bedrock (cross-region inference) pueden procesar en otra región UE.
- La cadena de hashes no está anclada fuera (quien controla el disco puede reescribirla entera).
- Sin verificación IBAN mod-97.
- Tiempos: ~30–70 s por paquete de 5 páginas sin caché; timeout Lambda 180 s.
