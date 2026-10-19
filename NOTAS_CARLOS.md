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

## Guía de prueba con Jose

Pre-requisitos: credenciales SSO de la **team account** en el entorno
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), región Frankfurt.
Activar venv: `source /workspace/hack/venv/bin/activate` y
`cd /workspace/hack/extractor`.

### Paso A — Salud del entorno
```bash
python -c "import boto3; print(boto3.client('sts', region_name='eu-central-1').get_caller_identity()['Arn'])"
```
Esperado: ARN con `ai-hackathon-TeamAdmin`.

### Paso B — CLI en paquete con documento faltante
```bash
python -m extractor samples/02_missing_payslip/package.pdf -o /tmp/kb02.json
python -c "import json; r=json.load(open('/tmp/kb02.json')); \
 print(r['recommended_next_step'], r['missing_documents'], r['fields_auto_validated_pct'], r.get('cost'))"
```
Esperado: `request_missing_documents`, `['payslip']`, auto ≈ 96%, `cost.total_usd` pequeño.

### Paso C — Qué mirar en el JSON
1. `documents[].doc_type` y `page_start` — ¿partió el PDF multi-doc?
2. `documents[].fields.<campo>.{value,textract,bedrock,confidence,page,status}`
   — si `status=manual_review`, mirad ambos valores.
3. `checks[]` — reglas deterministas (`birth_number_valid`, `income_*`, etc.).
4. `missing_documents` + `draft_client_message` — texto listo para el cliente.
5. `recommended_next_step` — lo que Main Sequence puede enrutar al clerk.
6. `cost` — gasto estimado de esa llamada.

### Paso D — API HTTP (integración Main Sequence)
```bash
uvicorn extractor.api:app --host 127.0.0.1 --port 8080 &
curl -s http://127.0.0.1:8080/health
curl -s -F "files=@samples/02_missing_payslip/package.pdf" \
  http://127.0.0.1:8080/extract | python -m json.tool | head -80
```
Esperado health: `{"status":"ok","version":"0.1.0"}`.
Extract: mismo JSON que la CLI.

### Paso E — Eval completa (cuesta ~0,45 USD de los 5)
```bash
# Solo si Carlos OK con el gasto restante del tope
for p in 01_clean 02_missing_payslip 03_amount_mismatch 04_name_typo_invalid_rc; do
  python -m extractor samples/$p/package.pdf -o samples/out/$p.json
done
python eval.py
```
Esperado: 4/4 OK, field_accuracy 1.0, check_accuracy 1.0.

### Paso F — Demo de 4 minutos (guion corto)
1. Subir `04_name_typo_invalid_rc/package.pdf`.
2. Mostrar clasificación por página + split.
3. Clic mental en `birth_number` del DNI → `discrepancy` (mod 11).
4. Mostrar `last_name_consistent: warning` (Novakowa vs Nováková).
5. Enseñar `draft_client_message` y `recommended_next_step`.
6. Cerrar: “el modelo nunca decide; cada cifra lleva motor + página + regla”.

---

## Archivos clave

- Motor: `/workspace/hack/extractor/extractor/`
- Notas: `/workspace/hack/extractor/NOTAS_CARLOS.md` (este archivo)
- Zip: `/workspace/hack/extractor.zip`
- Ledger de coste: `/workspace/hack/extractor/samples/out/cost_ledger.json`

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
