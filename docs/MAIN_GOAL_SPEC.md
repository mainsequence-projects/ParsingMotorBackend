# Motor de control documental KB — Objetivo, estado y especificación

*Komerční banka · FinTechers Hackathon · Track 6 · autor: Carlos (motor) · frontend/Main Sequence: Jose*
*Versión del documento: 19-oct-2026. Code freeze: martes 20-oct-2026, 13:30 (Praga).*

---

## 1. Objetivo principal

**La pregunta del reto:** ¿cómo puede la IA hacer el control back-office de los paquetes de
documentos de un préstamo más rápido y fiable, manteniendo cada decisión explicable y auditable?

**El caso (mercado checo).** El back-office recibe un **paquete**: un **Contrato** (úvěrová smlouva)
que dice qué documentos hacen falta, más **5–7 tipos de documento DISTINTOS**:

| Código | Documento (CZ) | Para qué |
|---|---|---|
| `contract` | Úvěrová smlouva | define el caso y la lista de documentos obligatorios |
| `id_card` | Občanský průkaz | identidad, rodné číslo, dirección |
| `bank_statement` | Výpis z účtu | titular, IBAN, abono de salario |
| `payslip` | Výplatní lístek | empleador, ingreso neto |
| `proof_of_address` | Doklad o bydlišti (factura) | domicilio |
| `employer_confirmation` | Potvrzení o zaměstnání a příjmu | empleador, antigüedad, ingreso |
| `tax_or_debt_certificate` | Potvrzení o bezdlužnosti (Finanční úřad) | sin deudas fiscales, fecha reciente |

El motor debe: leer cada página (escaneos, fotos, desordenadas), saber qué documento es,
extraer los datos **citando página y confianza**, comprobar con **reglas configurables** que el
paquete está completo y es coherente, proponer el **siguiente paso**, redactar la **petición al
cliente** (checo + inglés), dejar que una persona **confirme / edite / rechace / pida
documentos**, y guardar **todo** en un registro de auditoría. La IA lee; **el código decide**.

---

## 2. Matriz de trazabilidad

Leyenda: **Hecho** = funciona y tiene test/eval · **Parcial** = funciona con límites · **Falta**.
Dueño: **C** = Carlos (motor/API) · **J** = Jose (frontend + Main Sequence).

| # | Requisito del brief | Estado | Evidencia | Hueco | Dueño |
|---|---|---|---|---|---|
| P1 | Ingesta de paquete (PDF/imagen, multi-archivo) | Hecho | `ingest.py`, `POST /extract`, `/extract_json`, `tests/test_ingest.py`, `test_api.py` | — | C |
| P2 | OCR con enlace a la página original | Hecho | Textract QUERIES + LINES, `page`/`bbox` por campo, `inputs[].sha256` | bbox solo de Textract | C |
| P3 | Clasificación de páginas | Hecho | `classify.py` (Haiku 4.5); eval: clasificación por página (§3) | — | C |
| P4 | Bonus: separar/agrupar y páginas desordenadas | Hecho | `splitter.py`, paquete *shuffled*, `test_pipeline.py` | 2 docs del mismo tipo sin clave se fusionan | C |
| P5 | Extracción por tipo con cita y confianza | Hecho | `extract.py` + `reconcile.py` (`source`, `page`, `confidence`), eval campos | máx. 6 páginas por doc a Bedrock | C |
| P6 | 5–7 tipos distintos definidos por el Contrato | Hecho (v0.4) | `schemas.yaml` (7 tipos), `doclist.py`, paquetes nuevos | — | C |
| P7 | Motor de reglas configurable | Hecho | `rules.yaml` (versión sha256), `rules_engine.py`, `recheck`, `test_rules.py` | sin editor visual | C/J |
| P8 | Completitud vs Contrato | Hecho | `COMPLETENESS`, 2º lector determinista de la lista | — | C |
| P9 | Consistencia entre documentos | Hecho | `XDOC_*` (nombre, RČ, dirección, ingreso, empleador) | `_first`: compara el primer doc de cada tipo | C |
| P10 | Vínculos lógicos (fechas, vigencia) | Hecho | `RC_VALID`, `RC_BIRTHDATE_MATCH`, `ID_NOT_EXPIRED`, `DOC_RECENT`, `EMPLOYMENT_MIN_MONTHS` | — | C |
| P11 | Siguiente paso recomendado | Hecho | `next_step` en rules.yaml, eval next-step | — | C |
| P12 | Borrador de petición al cliente | Hecho | `draft_client_messages {cs,en}` | plantillas fijas (no LLM) | C |
| P13 | HITL confirmar/editar/rechazar/pedir | Parcial | `POST /runs/{id}/decisions`, `test_api_contract.py` | **la pantalla** es de Jose | J |
| P14 | Auditoría: versiones de input, regla/modelo, hora, historial humano | Parcial | `audit.py` cadena de hashes, `/verify`, `test_audit.py` | en Lambda vive en `/tmp` (efímero); no anclado fuera | C |
| P15 | Protocolo de control citado (HTML/PDF) | Hecho (v0.4) | `GET /runs/{id}/protocol`, CLI `protocol` | firma = manual | C |
| D1 | Demo: camino feliz | Hecho | paquete 01 → `approve_ready` | — | C/J |
| D2 | Demo: camino con error + petición | Hecho | paquetes con faltante/discrepancia → borrador | — | C/J |
| D3 | Demo: resolución humana + verify | Hecho | test `test_demo_story_...` | UI de Jose | J |
| E1 | Precisión | Hecho | `scripts/eval.py`, §3 | datos sintéticos | C |
| E2 | Trazabilidad / explicabilidad | Hecho | cada campo/check cita `rule_id`, página, valores de ambos motores | — | C |
| E3 | Robustez | Parcial | escaneo, foto, rotación 180°, página faltante, desorden | sin manuscritos ni sellos reales | C |
| E4 | Usabilidad back-office | Parcial | protocolo HTML, demo HTML mínima | UI final = Jose | J |
| G1 | Reglas separadas de la extracción y fáciles de cambiar | Hecho | `rules.yaml` + `recheck` sin AWS | — | C |
| G2 | Reglas que mejoran con decisiones reales | Parcial | `GET /learning` (sugerencias, nunca auto) | sin ciclo de aprobación de reglas | C |
| S1 | Paquetes sintéticos 5–7 docs, escaneos, páginas faltantes, datos incoherentes | Hecho (v0.4) | `scripts/gen_packages.py`, `samples/*/ground_truth.json` | — | C |

*(Los estados de esta tabla se actualizan al final de la jornada con el eval real; ver §3.)*

---

## 3. Qué tenemos

**Arquitectura:** ver `arquitectura_motor.png`.

```
Paquete ─► Ingesta ─► Clasificar (Haiku) ─► Agrupar ─► ┬─ Textract (OCR + confianza + bbox)
                                                         └─ Bedrock Sonnet (lectura visual)
                                ─► Reconciliador (código) ─► Reglas (rules.yaml) ─► Siguiente paso
                                ─► Borrador al cliente ─► Auditoría (cadena de hashes) ─► Protocolo
```

**Números medidos** — ver §3.1 (se rellena con el eval real de los paquetes nuevos).

### 3.1 Eval
(ver tabla al final del documento, sección "Resultados medidos")

---

## 4. Qué falta (ordenado por peso en la evaluación)

1. **Usabilidad back-office (pantalla de revisión)** — Jose: lista de casos, documento con el
   campo resaltado (página + bbox), botones confirmar/editar/rechazar/pedir, protocolo.
2. **Auditoría duradera en Lambda** — hoy `/tmp`. Requiere S3 o DynamoDB (tipo de recurso nuevo:
   **pedir permiso a Carlos**). Mitigación: copia de cada evento a CloudWatch Logs.
3. **Robustez con documentos reales** — sellos, manuscritos, fotos malas. Solo sintéticos.
4. **Ciclo de mejora de reglas** — hoy sugerencias; falta "proponer cambio de rules.yaml →
   revisión → nueva versión".
5. **Anclaje externo del hash de auditoría** (S3 Object Lock / firma KMS).

---

## 5. Especificación paso a paso (motor robusto)

Cada paso: módulo · entrada → salida · reglas deterministas · fallos y cómo se manejan · tests ·
criterio de aceptación.

### 5.1 Ingesta — `extractor/ingest.py`
- **Entrada:** 1..12 archivos (`PDF`, `PNG`, `JPEG`, `TIFF/WebP`), ≤15 MB c/u, ≤20 páginas total.
- **Salida:** `Page{source_file, page (1-based), png, sha256, source_sha256}` + `inputs[{name, original_name, bytes, sha256}]`.
- **Reglas:** tipo por *magic bytes*, no por extensión; nombres duplicados → `__2`; dpi 150
  (acotado en páginas gigantes); EXIF aplicado; límite 50 MP.
- **Fallos:** vacío 400; tipo 415; corrupto/cifrado 422; demasiado grande 413. **Nunca 500, y
  sin gastar AWS.**
- **Tests:** `test_ingest.py`, `test_api.py::test_bad_uploads_are_4xx_not_500`.
- **Aceptación:** todo input malo da 4xx con mensaje claro; hash de cada archivo y página en el audit.

### 5.2 Preprocesado
- Rotación 90/180/270 detectada por el clasificador y aplicada antes de extraer; re-codificación
  JPEG si una página supera el límite de Bedrock. Deskew fino no hace falta (±3° lo toleran ambos motores).
- **Aceptación:** paquete escaneado con una página al revés se lee igual que el limpio (eval).

### 5.3 Clasificación de páginas — `classify.py`
- **Salida por página:** `{doc_type ∈ DOC_TYPES, confidence, reason, page_number, page_count, doc_key, rotation, model}`.
- **Reglas:** tipo fuera de la lista → `other`; confianza robusta a texto; marcas "Strana x z y".
- **Fallos:** throttling → reintento y modelo de respaldo **marcado** (`MODEL_PRIMARY` = warning).
- **Aceptación:** ≥ 98 % de páginas bien clasificadas en el eval.

### 5.4 Agrupar / separar — `splitter.py`
- **Salida:** documentos `{doc_id, doc_type, source_file, doc_key, pages[]}`.
- **Reglas:** (archivo, tipo, `doc_key`); número de página repetido ⇒ otro documento; orden por
  marca impresa. Huecos en "x de y" ⇒ `missing_pages`.
- **Aceptación:** paquetes desordenados agrupados 100 %; página faltante detectada.

### 5.5 Extracción por tipo — `extract.py`, `schemas.yaml`
- **Por documento y en paralelo:** Textract `AnalyzeDocument QUERIES` (valor, confianza, página,
  bbox) y Bedrock Sonnet 4.5 (JSON estricto, valor + página). Lista de documentos del Contrato:
  además, líneas OCR + parser determinista (`doclist.py`).
- **Esquema por campo:** `type ∈ {name, text, address, date, birth_number, amount, iban, id, doc_list}`.
- **Fallos:** respuesta no-JSON del LLM ⇒ campo vacío (no tumba el run); caché por contenido.
- **Aceptación:** ≥ 97 % de campos correctos en el eval; cada valor con página.

### 5.6 Reconciliación — `reconcile.py` (código, no IA)
- **Salida por campo:** `{value, normalized, textract, textract_conf, bedrock, source, confidence, page, bbox, outcome, status, rule_ids}`.
- **Reglas (`FIELD_RECONCILE`):** iguales y conf ≥ 0,9 ⇒ passed; **iguales exactos y conf ≥ 0,5 ⇒
  passed** (doble lectura independiente); parecidos ⇒ warning; distintos ⇒ manual_review; OCR
  ilegible ⇒ manual_review; ninguno ⇒ missing.
- **Aceptación:** ningún valor erróneo con `passed` en el eval.

### 5.7 Reglas — `cross_checks.py` + `rules.yaml`
- **Completitud:** el Contrato siempre; lista del Contrato (o por defecto); tipos desconocidos ⇒ manual_review.
- **Consistencia:** nombre, apellido (typo ⇒ warning), rodné číslo, dirección, titular, ingreso
  (contrato ↔ nómina ↔ extracto ↔ confirmación del empleador, tolerancia 1 %), empleador
  (nómina ↔ confirmación).
- **Vínculos lógicos:** RČ válido (mod 11, offsets, fecha no futura), fecha nacimiento ↔ RČ,
  DNI vigente a la fecha del contrato, certificado y confirmación de empleo ≤ 30 días
  (configurable), antigüedad mínima en el empleo, certificado sin deudas.
- **Salida por check:** `{check_id, rule_id, rules_version, outcome, status, detail, evidence[{doc_id, field, value, page}]}`.
- **Aceptación:** 100 % de los problemas sembrados detectados, 0 falsas alarmas duras.

### 5.8 Petición al cliente
- Plantillas deterministas por regla, **checo + inglés** (`draft_client_messages.cs/en`), solo si
  falta algo o hay discrepancia. El LLM no redacta (no inventa promesas).
- **Aceptación:** cada faltante/discrepancia del paquete aparece en el borrador.

### 5.9 Protocolo de control — `GET /runs/{id}/protocol` (HTML/PDF), CLI `protocol`
- Cabecera (caso, run_id, hora, versiones motor/reglas/modelos, hashes de entrada), resumen por
  estado + siguiente paso, tabla por check (rule_id, descripción, estado, evidencia: doc, página,
  valor de cada motor, confianza), faltantes + borrador, historial humano, pie
  "AI output supports the decision; not an automatic final approval", firma.
- **Aceptación:** generable offline desde el audit; mismo contenido que el JSON.

### 5.10 API HITL — `api.py`
- `POST /runs/{id}/decisions` (confirm/edit/reject/request_documents) ⇒ evento añadido, nunca se
  modifica nada; re-evaluación determinista; `GET /runs/{id}` estado actual.
- **Aceptación:** historia de la demo (discrepancia → edición → approve_ready → verify ok) en test.

### 5.11 Auditoría — `audit.py`
- JSONL por run, cada evento con `seq, ts, type, payload, prev_hash, hash`; eventos: run_started
  (inputs + hashes, reglas completas, modelos, git), classification, extraction, evaluation,
  run_completed, decision, reevaluation. `GET /runs/{id}/verify`.
- **Aceptación:** cualquier edición del archivo ⇒ `ok:false`.

### 5.12 Bucle de aprendizaje — `GET /learning`
- Agrega decisiones humanas por `rule_id` (confirmaciones vs overrides) y sugiere cambios con
  soporte mínimo. **Nunca se aplica solo**: un humano cambia rules.yaml (nueva versión).

### 5.13 Contrato API para Jose
- `docs/API.md` (ES+EN), `docs/openapi.json` generado y verificado por test; respuestas validadas
  por modelos pydantic; errores 400/401/404/409/413/415/422/500/502/503.

### 5.14 Despliegue — `deploy/deploy.sh`
- Lambda + Function URL (AWS_IAM) + CloudFront (OAC). Conserva env y token. Solo eu-central-1.
- **Aceptación:** `/health` muestra versión y commit; 401 sin token; extract firmado OK.

### 5.15 Coste
- Medidor por run y ledger con tope (`EXTRACTOR_BUDGET_USD` ⇒ 503 al superarlo); caché por contenido.
- Coste medido por paquete: ver §3 (≈0,026 USD por página sin caché en v0.3).

---

## 6. Guion de demo (4 minutos) y preguntas

| Tiempo | Qué se ve | Qué se dice |
|---|---|---|
| 0:00–0:30 | Problema | "Un clerk tarda X min por caso comparando 7 documentos a mano." |
| 0:30–1:30 | Paquete feliz (7 docs) → `approve_ready` | clasificación, cada campo con página y confianza, ambos motores coinciden |
| 1:30–2:30 | Paquete con error → `manual_review_discrepancy` + borrador CZ/EN | la regla que salta, evidencia con página y valores |
| 2:30–3:15 | Clerk edita/confirma → `approve_ready`; `/verify` ok | auditoría: nada se borra, todo encadenado |
| 3:15–4:00 | Protocolo PDF citado + rules.yaml | "cambiar una regla = editar YAML, nueva versión, re-evaluar sin coste" |

**¿Por qué determinista?** Porque el banco tiene que poder explicar cada decisión: la IA lee
(dos lectores independientes), pero el valor final y el estado los decide código versionado y
testeado. El mismo input + las mismas reglas = el mismo resultado.

**¿Dónde se procesa?** AWS Frankfurt (eu-central-1): Lambda, Textract. Bedrock vía perfiles
`eu.*` (puede usar otras regiones de la UE; los datos no salen de la UE).

**GDPR:** datos solo sintéticos en el hackathon; en producción: minimización (solo campos del
esquema), retención configurable, no se entrena con los datos (Bedrock no usa inputs para
entrenar), auditoría de acceso y decisiones, derecho de revisión humana (art. 22: nunca
aprobación automática final).

**Coste por paquete:** medido ≈0,13 USD (5 páginas, sin caché) en v0.3; ver §3 para 7 documentos.

---

## 7. Plan hasta el code freeze (mar 20-oct 13:30, Praga)

| Cuándo | Qué | Dueño |
|---|---|---|
| lun 15:00–20:00 | 7 tipos, paquetes nuevos, protocolo, borrador CZ/EN, eval real, redeploy | Carlos |
| lun 20:00–22:00 | Jose conecta frontend a `/extract_json`, `/runs`, `/decisions`, `/protocol` | Jose |
| mar 08:00–10:00 | prueba conjunta con el guion (§6), bugs | Carlos + Jose |
| mar 10:00–12:00 | ensayo de la demo ×2, congelar `rules.yaml` | Carlos + Jose |
| mar 12:00–13:30 | solo fixes críticos; 13:30 code freeze; push cuando Carlos lo diga | Carlos |
