# Notas de Carlos — Motor de extracción KB (Track 6)

Documento de decisiones para el hackathon FinTechers / Komerční banka:
**Automating back-office document controls**. Autoria: Carlos.
Compañero: Jose (integra en Main Sequence vía API HTTP).

Región fija: **eu-central-1 (Frankfurt)**. Zona horaria de reportes: Europe/Prague.

---

## 1. Scaffold (schemas, normalización, reglas, ingest)

**Qué:** Paquete Python `extractor/` con `schemas.yaml`, `normalize.py`, `rules.py`
(rodné číslo), `ingest.py` (PDF→PNG con PyMuPDF), wrappers AWS.

**Por qué:** Separar lo determinista de las llamadas a la nube. Jose y yo podemos
probar normalización y rodné číslo sin gastar. Alternativa rechazada: un solo
prompt gigante a GPT que “lo haga todo” — el jurado y un regulador no pueden
auditar eso. Criterio hackathon: trazabilidad y reglas fijas.

**Cómo probar:**
```bash
source /workspace/hack/venv/bin/activate
cd /workspace/hack/extractor
python -c "from extractor.rules import parse_birth_number as p; \
 print(p('8854120000')); print(p('8854120001'))"
```
Esperado: el primero `valid: True` (fecha 1988-04-12, sex F); el segundo `valid: False`.

**Límites:** La regla mod-11 del rodné číslo cubre el caso estándar de 10 dígitos;
casos legacy raros pueden requerir ampliación.

---

## 2. Motor dual + reconciliador + API

**Qué:** `classify.py`, `extract.py` (Textract QUERIES ∥ Bedrock Converse),
`reconcile.py`, `cross_checks.py`, `pipeline.py`, CLI y FastAPI (`/extract`, `/health`).

**Por qué:** Dos motores suben el score; el código decide el valor. Alternativa
rechazada: un tercer LLM que “elija el correcto” — volveríamos a depender de un
modelo. Criterio: doble motor + flujo humano en excepciones + superficie para Jose.

**Cómo probar (sin AWS de paquete completo):** ver sección Guía con Jose.

**Límites:** Textract Queries a veces asocia un importe cercano (préstamo vs ingreso).
Por eso en `amount`, OCR solo gana si conf ≥ 0.9; si no, usamos Bedrock y marcamos
`manual_review`.

---

## 3. Samples sintéticos + eval

**Qué:** 4 paquetes PDF inventados (Jana Nováková) + `eval.py` contra `ground_truth.json`.

| Paquete | Intención |
|---------|-----------|
| 01_clean | Todo coherente |
| 02_missing_payslip | Falta payslip |
| 03_amount_mismatch | Ingreso 52000 vs 38000 |
| 04_name_typo_invalid_rc | Typo apellido + RC inválido |

**Por qué:** El reto de KB pide datos propios. Nunca datos reales. Alternativa
rechazada: fotos de DNIs de internet. Criterio: precisión **medida** en la demo.

**Cómo probar:**
```bash
python scripts/gen_samples.py
python -m extractor samples/01_clean/package.pdf -o samples/out/01_clean.json
python eval.py
```
Esperado (medido 2026-10-19): `packages_fully_correct: 4/4`, `field_accuracy: 1.0`,
`check_accuracy: 1.0`.

**Límites:** Los PDF sintéticos con reportlab son “limpios”; documentos escaneados
reales tendrán más `manual_review`. Formato checo `52 000` confunde OCR — usamos
`52000.00 CZK` en samples.

---

## 4. Región Frankfurt (eu-central-1)

**Qué:** Todos los clientes boto3 usan `region_name='eu-central-1'`. Si
`AWS_REGION` apunta a otra región, el proceso termina con código 2.

**Por qué:** Exigencia de Carlos / cuenta del hackathon.

**Prueba de model ID in-region:**
- `anthropic.claude-sonnet-4-5-20250929-v1:0` → **FAIL** ValidationException
  (on-demand no soportado; hace falta inference profile).
- `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` → **OK**.

**Honestidad sobre `eu.*`:** la llamada se hace **desde Frankfurt**, pero Bedrock
Cross-Region Inference puede procesar en otras regiones de la **UE**. Los datos
permanecen en la UE, no garantizamos “solo Frankfurt”. Alternativa rechazada:
inventar que el perfil `eu.*` es single-region.

**Cómo probar:**
```bash
AWS_REGION=us-east-1 python -m extractor samples/01_clean/id_card.pdf
# esperado: ERROR ... refuses to run outside Frankfurt, exit 2
```

---

## 5. Coste (tope 5 USD de build; cuenta máx. 800 USD)

**Reglas de Carlos:**
- (a) Cero recursos provisionados (sin endpoints, throughput, adapters, EC2,
  Lambda, API GW, OpenSearch, KB, buckets S3). Solo `AnalyzeDocument` sync y
  `Converse` pay-per-call.
- (b) Tope de este build: **5 USD**. Medidor en `extractor/cost.py`; aborta si se
  supera. Ledger: `samples/out/cost_ledger.json`.
- (d) Haiku 4.5 para clasificar; Sonnet 4.5 solo para extracción.

**Precios usados (estimación list-price):**
- Textract Queries: **0,015 USD / página**
- Claude Haiku 4.5: 0,80 / 4,00 USD por 1M tokens (in/out)
- Claude Sonnet 4.5: 3,00 / 15,00 USD por 1M tokens (in/out)

**Gasto estimado total de este build: ~2,45 USD** (bajo el tope de 5).
- Histórico (suites E2E antes del medidor + smoke + API): **~2,35 USD**
- Verificación post-medidor (paquete 02, medido): **0,0964 USD**

### Coste por paquete documental

| Paquete | Páginas | USD | Nota |
|---------|---------|-----|------|
| 01_clean | 5 | ~0,12 | estimado a partir de 02 |
| 02_missing_payslip | 4 | **0,0964** | medido con Haiku+Sonnet |
| 03_amount_mismatch | 5 | ~0,12 | estimado |
| 04_name_typo_invalid_rc | 5 | ~0,12 | estimado |

Desglose típico paquete 4 páginas: Textract ~0,06 USD + Bedrock ~0,036 USD.

**Cómo probar el medidor:**
```bash
python -c "from extractor.cost import snapshot; print(snapshot())"
# tras un extract: total_usd > 0 y remaining_usd < 5
```
Si se supera el tope: excepción `BudgetExceeded` y STOP (pedir OK a Carlos).

**Límites:** Son estimaciones de list-price; la factura real de AWS puede variar
(CRIS, descuentos). No crea ningún recurso permanente.

---

## 6. Resultados medidos (eval real contra AWS)

> Histórico (4 paquetes, eval antiguo y más laxo). Resultados actuales: **§12**.

Fecha: 2026-10-19 (Europe/Prague).

```
packages_fully_correct: 4 / 4
field_accuracy: 1.0
check_accuracy: 1.0
```

| Paquete | next_step | auto % | missing |
|---------|-----------|--------|---------|
| 01_clean | spot_check_warnings | 97.0 | — |
| 02_missing_payslip | request_missing_documents | 96.3 | payslip |
| 03_amount_mismatch | manual_review_discrepancy | 97.0 | — |
| 04_name_typo_invalid_rc | manual_review_discrepancy | 93.9 | — |

`01_clean` no llega a `approve_ready` porque Textract a veces lee mal el importe
declarado (status `manual_review` en 1 campo) aunque el valor final (Bedrock) es
correcto y las reglas cross-doc pasan. Eso es un spot-check honesto, no un falso
approve.

Salidas: `samples/out/*.json`, `samples/out/eval_summary.json`,
`samples/out/example_snippet.json`.

---

## 7. Commits (identidad local)

```
git config user.name "Carlos"
git config user.email "233920722+aisona-lab@users.noreply.github.com"
```
Solo config **local** del repo (no global). Cada commit explica el POR QUÉ en español.

---

## 8. Despliegue en AWS (Lambda + Function URL + CloudFront)

**Qué se creó (eu-central-1):**

| Recurso | Nombre / ID |
|---------|-------------|
| Lambda | `fintechers-kb-extractor` (python3.12, 1024 MB, timeout 180s) |
| Function URL | AuthType **AWS_IAM** (ver por qué abajo) |
| IAM role ejecución | `fintechers-kb-extractor-role` (Textract + Bedrock eu.* + Logs + AssumeRole demo) |
| IAM role demo | `fintechers-kb-extractor-demo-invoke` (solo InvokeFunctionUrl; sesiones 15 min) |
| Log group | `/aws/lambda/fintechers-kb-extractor` (retención **7 días**) |
| CloudFront | distribución `E13GPX9M1FZKBI` → `d31su4idgeitei.cloudfront.net` |
| OAC | `fintechers-kb-extractor-oac` |
| ORP custom | `fintechers-kb-extractor-orp` |

Tags en todos: `project=fintechers-kb`, `owner=carlos`.

**URL pública (para Jose / jurado):**
`https://d31su4idgeitei.cloudfront.net/`

**Token API:** solo en `/workspace/hack/extractor/.deploy_token` (gitignored). No va al repo ni a estas notas.

### Por qué no AuthType NONE

Carlos pidió Function URL con AuthType NONE + header `x-api-key`. En esta cuenta/org,
las invocaciones anónimas a Function URL devuelven **403 Forbidden** aunque la resource
policy permita `Principal: *` (casi seguro un SCP). Lo comprobamos: con AWS_IAM + SigV4
responde 200; sin firma, 403.

**CloudFront + OAC** da una URL pública para GET (`/`, `/health`, `/session`). Los POST
con cuerpo firman mal a través de OAC (bug/limitación conocida SigV4 + body). Por eso la
página demo hace:
1. `GET /session` (CloudFront) con `x-api-key` → credenciales STS de 15 min
2. `POST /extract_json` **directo** a la Function URL firmado SigV4 en el navegador (aws4fetch)

### Coste del despliegue

- Idle: **~0 USD** (Lambda/CloudFront free tier; sin NAT/VPC/provisioned).
- Test de verificación (1 página id_card): **~0,024 USD** medidos por el cost meter.
- Reserved concurrency=2: **no se pudo** — la cuenta solo tiene 10 ejecuciones concurrentes
  totales y AWS exige dejar Unreserved ≥ 10. Documentado; no es opcional en este account.

### Cómo probar

```bash
PUBLIC=https://d31su4idgeitei.cloudfront.net/
TOKEN=$(cat /workspace/hack/extractor/.deploy_token)

# 1) Health abierto
curl -sS "$PUBLIC/health"

# 2) HTML demo
curl -sS "$PUBLIC" | head

# 3) Sin token → 401
curl -sS -o /dev/null -w "%{http_code}\n" "$PUBLIC/session"

# 4) Extract firmado (recomendado)
cd /workspace/hack/extractor
source ../venv/bin/activate
python deploy/signed_extract.py samples/02_missing_payslip/id_card.pdf | python -m json.tool | head
```

La página HTML en `$PUBLIC` pide el token, sube archivos y muestra el JSON.

### Cómo tirar todo abajo

```bash
bash /workspace/hack/extractor/deploy/teardown.sh
```

Borra CloudFront (disable+delete), OAC, ORP, Lambda URL, función, log group, roles IAM
(ejecución + demo-invoke) y cualquier bucket temporal de empaquetado.

### Scripts

- `deploy/deploy.sh` → `deploy.py` (Lambda) + `deploy_cloudfront.py` (público)
- `deploy/teardown.sh` → `teardown.py`
- `deploy/signed_extract.py` → helper de prueba sin navegador

### Límites honestos

- Origin read timeout CloudFront = 60s (paquetes muy grandes pueden cortarse; usar
  `signed_extract.py` directo a Lambda evita ese tope).
- No se usa API Gateway (límite 29s).
- Empaquetado: zip 36 MB (bajo 50 MB; no hizo falta S3 permanente).

---

## 9. Reglas en configuración (`extractor/rules.yaml`)

**Qué:** todos los umbrales y checks salen de Python a `extractor/rules.yaml`
(15 reglas, `schema_version: 1`, label `kb-rules-2026-10-19`). Cada regla tiene `id`,
`description` es/en, `severity` (outcome → `passed | warning | manual_review |
discrepancy | missing`) y `params`. Incluye: reconciliador (`FIELD_RECONCILE`:
`pass_conf 0.9`, en importes Bedrock gana si Textract conf < 0.9), rodné číslo
(`RC_VALID`: mod 11, offsets de mes +50/+70 mujer, +20 hombre, pivote de siglo),
fecha de nacimiento vs RČ, cruces nombre/RČ/dirección/ingresos (tolerancia 1%),
documentos requeridos por defecto, continuidad de páginas, documentos no pedidos y
la política de siguiente paso (`next_step.order`, gana la primera que se cumple).
`rules_engine.py` valida el esquema al cargar y lanza `RulesConfigError` si falta una
regla, un param, una descripción, una severidad no permitida o un umbral fuera de [0,1].
Cada check del JSON lleva `rule_id` y `rules_version` (sha256[:16] del fichero) y cada
campo lista sus `rule_ids`. Flag CLI `--rules ruta.yaml` (API: env `EXTRACTOR_RULES`).
`python -m extractor recheck <run_id> --rules otra.yaml` re-aplica reglas a un run
guardado **sin llamar a AWS**.

**Por qué:** riesgo/compliance del banco tiene que poder leer y cambiar una regla sin
desplegar código, y cada decisión tiene que poder reproducirse con la versión exacta.

**Alternativas descartadas:** constantes en Python (opaco para negocio); motor de
reglas genérico tipo DSL/Drools (demasiado para 48 h); reglas en base de datos
(recurso provisionado, prohibido por presupuesto).

**Criterio hackathon:** explicabilidad y auditabilidad; adaptabilidad a nuevos
productos de crédito.

**Cómo probar:**
```bash
python -m pytest -q tests/test_rules.py
# esperado: todos pasan; test_changing_income_tolerance_changes_outcome: 0.01 -> 0.30
# convierte income_contract_vs_payslip de discrepancy a passed
python -c "import yaml;d=yaml.safe_load(open('extractor/rules.yaml'));\
[r for r in d['rules'] if r['id']=='XDOC_INCOME'][0]['params']['tolerance']=0.30;\
yaml.safe_dump(d,open('/tmp/rules_tol30.yaml','w'),allow_unicode=True,sort_keys=False)"
python -m extractor recheck <run_id_del_03> --rules /tmp/rules_tol30.yaml
```
Medido hoy (run 03 `20261019T122100Z-9ad003f8`): `rules_version e5467f7b0adf0668 →
e667d640ddf6c2dd`, los 2 checks `XDOC_INCOME` pasan de `discrepancy` a `passed`, y
`next_step` de `manual_review_discrepancy` a `spot_check_warnings`.

**Límites:** las reglas son parametrizables, no programables (añadir un *tipo* de
regla nuevo sigue requiriendo Python + test). La política de next_step es una lista
ordenada simple.

---

## 10. Audit trail (registro encadenado + decisiones humanas)

**Qué:** cada run escribe `audit/<run_id>.jsonl` (append-only) + `audit/index.jsonl`.
Eventos: `run_started` (run_id, hora UTC ISO, versión motor 0.2.0, commit git + flag
dirty, rules_version + texto de reglas embebido, modelos Haiku/Sonnet, Textract
`AnalyzeDocument` sync `QUERIES` api_version `2018-06-27`, sha256 de cada input y de
cada imagen de página), `classification`, `extraction` (valores crudos de ambos
motores por documento + cache hits), `evaluation` (por campo: valor/conf/página
Textract, valor/página Bedrock, valor final, status, rule_ids; checks; next_step),
`run_completed` (coste del run). Cada evento guarda `prev_hash` y su `hash` =
sha256 del JSON canónico; `verify` detecta líneas modificadas, borradas o reordenadas.
Decisiones humanas: `POST /runs/{run_id}/decisions` o `python -m extractor decide` con
`{field|check, action: confirm|edit|reject|request_documents, new_value?, reviewer,
comment}`: se añade un evento `decision` (con el valor del motor *antes*) y un
`reevaluation` con el nuevo siguiente paso. Nunca se reescribe nada; con la cadena
rota se niega a añadir (HTTP 409). `GET /runs/{id}`, `GET /runs/{id}/verify`,
`GET /learning`. Informe de aprendizaje: agrega overrides por `rule_id` y sugiere
cambios (umbral `pass_conf`, severidad por outcome, revisar regla con ≥50% overrides);
`auto_applied: false` siempre.

**Por qué:** en back-office bancario hay que poder reconstruir quién/qué decidió cada
cosa y con qué reglas, y demostrar que nadie lo ha retocado después.

**Alternativas descartadas:** QLDB / blockchain (recurso provisionado y QLDB está en
fin de vida); base de datos con UPDATE (mutable); firmar con KMS (coste y recurso
extra; el hash chain basta para la demo, ver límites).

**Criterio hackathon:** auditabilidad, human-in-the-loop, mejora continua.

**Cómo probar:**
```bash
python -m pytest -q tests/test_audit.py           # verify ok, copia manipulada falla
python -m extractor verify samples/out/audit_example_05_scanned_noisy.jsonl   # ok: true, exit 0
cp samples/out/audit_example_05_scanned_noisy.jsonl /tmp/t.jsonl
sed -i '8s/manual_review_discrepancy/approve_ready/' /tmp/t.jsonl
python -m extractor verify /tmp/t.jsonl            # ok: false, "hash mismatch", exit 2
python -m extractor learn --min-support 1          # sugerencias, auto_applied: false
```
Medido hoy vía API local: run `20261019T122339Z-ccd480db` (05) → edit humano de
`contract.declared_monthly_income` a 52000 → `next_step manual_review_discrepancy →
spot_check_warnings`; verify ok (11 eventos). La copia manipulada del run 03 falla con
`line 7: hash mismatch (content modified)`.

**Límites:** el hash chain prueba integridad *interna*; quien tenga acceso de escritura
podría reescribir el fichero entero y recalcular hashes. En producción: anclar el
`head_hash` fuera (S3 Object Lock / firma KMS / WORM). Los logs contienen datos de
documentos (aquí solo sintéticos): `audit/` está en `.gitignore`. El audit vive en el
disco local; en Lambda iría a `/tmp` (efímero) → la Lambda desplegada NO se ha
actualizado (sigue en 0.1.0, sin audit).

---

## 11. Paquetes difíciles 05-09 + splitter nuevo

**Qué:** `scripts/gen_hard_samples.py` (no toca los PDF 01-04, solo amplía su
ground truth): 05 escaneado ruidoso (01 a 100 dpi, ±3°, ruido, JPEG q40, pág. 3 al
revés); 06 páginas barajadas en un PDF con extracto de 2 páginas ("Page x of 2", IBAN en
ambas); 07 falta la página 2 de 2 del extracto; 08 documentos no pedidos
(proof_of_address + certificado de seguro); 09 foto torcida del DNI sobre fondo con
perspectiva (`contract.pdf` + `id_photo.jpg`). Clasificación por página con Haiku que
devuelve además `page_number/page_count` (solo si hay marcador impreso), `doc_key`
(IBAN, nº contrato, nº documento) y `rotation` (se endereza con PIL antes de extraer).
`splitter.py` agrupa por (fichero, tipo, doc_key), ordena por nº de página y un nº de
página repetido abre otro documento. `PAGE_CONTINUITY` detecta huecos → página
faltante en `missing_pages` + email al cliente; `UNEXPECTED_DOCUMENTS` avisa de lo no
pedido. Bedrock indica en qué imagen leyó cada campo (página por motor).

**Por qué:** lo real llega escaneado, girado, mezclado e incompleto; un splitter por
páginas contiguas rompe documentos barajados.

**Alternativas descartadas:** Textract `AnalyzeLending` / `StartDocumentAnalysis`
(asíncrono, necesita S3 → recurso nuevo); clasificador entrenado (sin datos ni tiempo);
deskew por OpenCV (Textract y Bedrock ya toleran ±3°; solo la rotación 90/180 importa).

**Criterio hackathon:** robustez con documentos reales; completitud.

**Cómo probar:**
```bash
python -m pytest -q tests/test_splitter.py
python scripts/gen_hard_samples.py      # regenera 05-09; 01-04 PDF sin cambios
EXTRACTOR_CACHE=1 python -m extractor samples/07_missing_page/package.pdf -o /tmp/07.json
# esperado: missing_pages=[bank_statement página 2 de 2], next=request_missing_documents
```

**Límites:** dos documentos del mismo tipo sin clave ni marcador (p. ej. 2 nóminas de
meses distintos) se fusionan. Haiku inventó una vez un `page_number: 2` en una página
sin marcador (05, pág. 3; no tuvo efecto porque `page_count` vino vacío). Los datos
son generados por nosotros: no sustituyen un set real del banco.

---

## 12. Resultados medidos (eval real contra AWS, 9 paquetes)

Fecha: 2026-10-19, ~14:20 Europe/Prague. `python scripts/eval.py --cache`. Reglas
`e5467f7b0adf0668`. Métricas estrictas (no se dan por buenos pasos "parecidos").

| Paquete | Págs ok | Agrup. | Campos | Flags esperados | Falsas alarmas | next_step (esperado) | USD real |
|---|---|---|---|---|---|---|---|
| 01_clean | 5/5 | ok | 33/33 | 0/0 | 0 | spot_check_warnings (approve_ready) | 0,1319 |
| 02_missing_payslip | 4/4 | ok | 27/27 | 1/1 | 0 | request_missing_documents ✔ | 0 (100% caché = págs de 01) |
| 03_amount_mismatch | 5/5 | ok | 33/33 | 2/2 | 0 | manual_review_discrepancy ✔ | 0,0518 |
| 04_name_typo_invalid_rc | 5/5 | ok | 33/33 | 3/3 | 0 | manual_review_discrepancy ✔ | 0,0522 |
| 05_scanned_noisy | 5/5 | ok | 32/33 | 0/0 | 2 | manual_review_discrepancy (approve_ready) | 0,1320 |
| 06_shuffled | 5/5 | ok | 29/29 | 0/0 | 0 | spot_check_warnings (approve_ready) | 0,0762 |
| 07_missing_page | 3/3 | ok | 23/23 | 1/1 | 0 | request_missing_documents ✔ | 0,0373 |
| 08_extra_document | 5/5 | ok | 27/27 | 1/1 | 0 | spot_check_warnings ✔ | 0,0304 |
| 09_photo_id | 2/2 | ok | 17/17 | 0/0 | 0 | spot_check_warnings (approve_ready) | 0,0547 |

Totales: clasificación por página **39/39 (1.0)**; agrupación **9/9**; campos
**254/255 (0.996)**; flags esperados **8/8**; falsas alarmas **2** (en 1 paquete);
next_step exacto **5/9**, "seguro" (igual o más conservador) **9/9**; documentos
faltantes 9/9, páginas faltantes 9/9, documentos no pedidos 9/9.

Lectura honesta:
- **05:** con el escaneo Textract no devolvió el ingreso declarado y Bedrock leyó
  **182000** (real 52000) → campo `llm_only`/warning; el cruce de ingresos lo marca como
  discrepancia ⇒ acaba en revisión manual (seguro, pero el campo está mal y son 2 falsas
  alarmas). El informe de aprendizaje sugiere endurecer `llm_only` a `manual_review` —
  **no aplicado** (decisión de Carlos).
- **01/06/09 no llegan a approve_ready:** varios campos coinciden en ambos motores pero
  con conf Textract 0,68–0,88 < `pass_conf 0.9` → warning → spot-check. Es la política
  conservadora tal cual; el eval antiguo lo contaba como acierto, este no.
- Los 01-04 los generamos nosotros con la misma plantilla: 100% ahí no prueba generalización.

Salidas: `samples/out/*.json`, `samples/out/eval_summary.json`,
`samples/out/audit_example_05_scanned_noisy.jsonl`.

---

## 13. Coste de esta tarea (tope 1 USD) y acumulado

**Qué:** ledger acumulado entre procesos (`EXTRACTOR_COST_LEDGER`, aquí
`.cost/task_2026-10-19.json`, gitignored) con `EXTRACTOR_BUDGET_USD=1.0`; `precheck()`
aborta antes de la llamada a AWS. Caché por contenido opcional (`--cache` /
`EXTRACTOR_CACHE=1`, `.cache/aws`, gitignored): clave = sha256(imagen) + prompt/queries
+ modelo; temperatura 0 ⇒ misma página, misma respuesta, se paga una vez. Cada run
informa coste propio y cache hits.

**Por qué:** el tope de 1 USD no aguantaba re-ejecutar 9 paquetes a ~0,024 USD/página
sin reutilizar las páginas idénticas.

**Alternativas descartadas:** bajar a Nova Lite para extraer (peor calidad);
eval con mocks (no serían números reales).

**Medido:** **0,5665 USD** en esta tarea (21 páginas Textract, 43 llamadas Bedrock,
86 628 tokens in / 7 446 out; 27 hits Textract y 53 hits Bedrock en caché). Acumulado
del build: ~2,47 USD previos (estimado, §5 y §8) + 0,5665 = **≈3,04 USD**, bajo 5 USD.
Nada creado ni modificado en AWS en esta tarea; la Lambda no se ha redesplegado.

**Cómo probar:** `python -c "from extractor.cost import snapshot; print(snapshot())"`
con las mismas env vars.

**Límites:** precios list-price estimados; la caché guarda respuestas con datos de
documentos → solo para datos sintéticos/dev; con la caché, el coste por paquete
depende del orden de ejecución (02 sale gratis porque reusa las páginas de 01).

---

## Archivos clave

- Motor: `extractor/` (reglas: `rules.yaml`, `rules_engine.py`; audit: `audit.py`;
  splitter: `splitter.py`; evaluación pura: `evaluate.py`)
- Tests: `tests/` (31 tests, sin AWS)
- Notas: `NOTAS_CARLOS.md` (este archivo)
- Samples + eval: `samples/`, `scripts/gen_samples.py`, `scripts/gen_hard_samples.py`, `scripts/eval.py`
- Zip: `/workspace/hack/extractor.zip`

---

## Guía de prueba con Jose

Pre-requisitos: credenciales SSO de la team account en el entorno, región Frankfurt.
```bash
cd ParsingMotorBackend && python -m venv .venv && source .venv/bin/activate
pip install -e . pytest
export AWS_REGION=eu-central-1 EXTRACTOR_BUDGET_USD=1.0 EXTRACTOR_CACHE=1
```

### Paso A — Tests sin AWS (gratis)
```bash
python -m pytest -q
```
Esperado: `31 passed`.

### Paso B — Salud AWS
```bash
python -c "import boto3; print(boto3.client('sts', region_name='eu-central-1').get_caller_identity()['Arn'])"
```

### Paso C — CLI con página faltante (≈0,04 USD sin caché)
```bash
python -m extractor samples/07_missing_page/package.pdf -o /tmp/07.json
```
Esperado en stderr: `next=request_missing_documents pages_missing=[{... 'missing_page_numbers': [2]}]`
y el `run_id`.

### Paso D — Qué mirar en el JSON
1. `pages_classified[]` — tipo, `page_number/page_count`, `doc_key`, `rotation` por página.
2. `documents[].pages` — cómo se agruparon y ordenaron.
3. `documents[].fields.<campo>` — `textract/textract_conf/textract_page`,
   `bedrock/bedrock_page`, `value`, `status`, `rule_ids`.
4. `checks[]` — `rule_id`, `rules_version`, `outcome`, `status`, `evidence`.
5. `missing_documents`, `missing_pages`, `unexpected_documents`, `draft_client_message`.
6. `recommended_next_step`, `cost.run`, `audit_file`.

### Paso E — Decisión humana + verify (gratis)
```bash
python -m extractor decide <run_id> --action request_documents --new-value bank_statement --reviewer jose
python -m extractor verify <run_id>        # ok: true
```

### Paso F — API (Main Sequence)
```bash
uvicorn api.kb_extractor.main:app --port 8080 &
curl -s localhost:8080/health                       # version 0.2.0
curl -s -F "files=@samples/05_scanned_noisy/package.pdf" localhost:8080/extract > /tmp/r.json
RID=$(python -c "import json;print(json.load(open('/tmp/r.json'))['run_id'])")
curl -s -X POST localhost:8080/runs/$RID/decisions -H 'content-type: application/json' \
  -d '{"field":"contract.declared_monthly_income","action":"edit","new_value":"52000","reviewer":"jose","comment":"leído del original"}'
curl -s localhost:8080/runs/$RID/verify
curl -s localhost:8080/learning
```
Esperado: la decisión devuelve `next_step_before` y `next_step`; verify `ok: true`;
learning con `auto_applied: false`.

### Paso G — Cambiar una regla sin tocar código (gratis)
Copiar `extractor/rules.yaml`, cambiar `XDOC_INCOME.params.tolerance` a 0.30 y
`python -m extractor recheck <run_id_03> --rules copia.yaml` → los checks de ingresos pasan.

### Paso H — Eval completa (≈0,57 USD con caché vacía; 0 con caché llena)
```bash
python scripts/eval.py --cache
```
Esperado: la tabla del §12 (con LLM pueden variar ligeramente los valores).

### Paso I — Demo de 4 minutos
1. Subir `06_shuffled` → enseñar cómo se reordena el extracto (págs 5 y 1).
2. `07_missing_page` → email al cliente pidiendo la página 2 de 2.
3. `04` → `RC_VALID` discrepancy citando `rule_id` + `rules_version`.
4. Decisión humana por API → nuevo next_step; `verify` ok; copia manipulada → falla.
5. Cerrar: "el modelo nunca decide; cada cifra lleva motor + página + regla + hash".
