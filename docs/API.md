# KB Document Control Extractor — API v0.3.0

> ES: Contrato HTTP para Jose (frontend + Main Sequence). La fuente de verdad formal es
> [`docs/openapi.json`](openapi.json) (generado con `python scripts/export_openapi.py`; un
> test falla si queda desactualizado). Todas las respuestas se validan con los modelos de
> `extractor/api_models.py`: se pueden **añadir** claves en el futuro, nunca quitar.
>
> EN: HTTP contract for Jose (frontend + Main Sequence). The formal source of truth is
> [`docs/openapi.json`](openapi.json). Every response is validated against the models in
> `extractor/api_models.py`: keys may be **added** later, never removed.

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
| GET | `/runs/{run_id}` | key | Estado actual (motor + decisiones humanas) | Current state |
| POST | `/runs/{run_id}/decisions` | key | Añade decisión humana y recalcula | Append human decision, re-evaluate |
| GET | `/runs/{run_id}/verify` | key | Verifica la cadena de hashes | Verify hash chain |
| GET | `/learning?min_support=3` | key | Overrides humanos por regla (sugerencias, nunca auto) | Human overrides per rule |
| GET | `/rules` | key | rules.yaml activo + versión | Active rules.yaml + version |
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
| `draft_client_message` | borrador de email al cliente (inglés) o `null` | client email draft or null |
| `models`, `rules_version`, `engine_version`, `git_commit`, `inputs[]` (sha256) | trazabilidad | provenance |
| `cost` | coste del run y caché | run cost |

`field.status`: `passed` · `warning` · `manual_review` · `missing`.
`check.status`: `passed` · `warning` · `discrepancy` · `manual_review`.
`field.outcome` (ejemplos): `agree_high_conf`, `agree_exact`, `agree_low_conf`,
`disagree`, `ocr_unparseable`, `llm_only`, `textract_only`, `both_missing`.

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

---

## 3. `recommended_next_step`
Orden de prioridad (rules.yaml → `next_step`), primero que aplica:

| Valor | Cuándo (ES) | When (EN) |
|---|---|---|
| `request_missing_documents` | falta un documento requerido o páginas | required doc/pages missing |
| `manual_review_discrepancy` | algún check en `discrepancy` (p. ej. ingresos ≠, RČ inválido, nombre ≠) | a cross-check discrepancy |
| `manual_review_extraction` | muchos campos en revisión manual o algún campo requerido sin valor | extraction not trustworthy |
| `spot_check_warnings` | solo avisos / un campo dudoso | warnings only |
| `approve_ready` | todo `passed` | all passed |

`draft_client_message` se rellena cuando hay algo que pedir al cliente.

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
- ES: audit en Lambda en `/tmp` (efímero y por contenedor): un `run_id` puede dar 404 en otra
  invocación. Para producción: S3/DynamoDB. EN: Lambda audit is ephemeral/per container.
- Guardia de presupuesto en Lambda es por contenedor (no global).
- Solo las primeras 6 páginas de un documento van a Bedrock (`MAX_BEDROCK_PAGES`).
- Dos documentos del mismo tipo sin clave distintiva se fusionan; los checks cruzados usan el
  primero de cada tipo.
- `draft_client_message` solo en inglés.
- Perfiles `eu.*` de Bedrock (cross-region inference) pueden procesar en otra región UE.
- La cadena de hashes no está anclada fuera (quien controla el disco puede reescribirla entera).
- Sin verificación IBAN mod-97.
- Tiempos: ~30–70 s por paquete de 5 páginas sin caché; timeout Lambda 180 s.
