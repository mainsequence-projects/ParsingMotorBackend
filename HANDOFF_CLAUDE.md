# HANDOFF — KB Document Control Engine (Track 6)

> **Resumen (ES).** Motor de control documental back-office para Komerční banka (mercado checo).
> Rama local `feat/kb-extractor`, **sin push** hasta que Carlos lo diga. Commits como Carlos con
> cuerpo en español explicando el *porqué*. Solo `eu-central-1`, solo datos sintéticos, nunca
> secretos en git (el token de la demo vive solo en `.deploy_token`). Presupuesto: 800 USD cuenta,
> tope 20 USD para el build. Code freeze **mar 20-oct-2026 13:30 (Praga)**. Este archivo es para
> que otra IA (Claude) continúe sin contexto: lee las secciones 4, 9, 10 y 12 primero.
>
> **Estado (19-oct 16:00):** v0.4.0 desplegada en la MISMA Lambda (commit `7c604b0`, env y token
> conservados), verificada por CloudFront (/health, extract firmado, protocolo HTML+PDF). 7 tipos
> checos, 12 paquetes sintéticos, protocolo citado, borrador CZ+EN. Eval real SIN caché: campos
> 98,3 %, checks 96,9 %, problemas sembrados 13/13, siguiente paso 7/12 exacto y 12/12 seguro.
> Gasto de esta tarea 5,74 USD; acumulado ≈ 9,10 USD (tope 20). Hallazgo clave: **Textract no lee
> checo** (pierde ř/č/š/í…); se compensa con reglas deterministas.

Last updated: 2026-10-19 16:10 Europe/Prague. Deployed: v0.4.0 commit 7c604b0. Local HEAD adds only docs commits after it (handoff, spec md/docx/pdf, NOTAS §17-20); no code change since the deploy. Keep this file updated after every commit.
**19-oct ~18:00 — v0.5.0 on local branch `feat/engine-mockup-parity`** (from origin/main `99a7a8f`, which already contains PR #1 rules proposals and Jose's typed endpoints). NOT pushed, NOT deployed (Lambda still runs 0.4.0). Adds the engine side of the mockups: `GET /runs`, `GET /runs/{id}/events`, `GET /runs/{id}/pages/{n}.png` (inputs now kept next to the audit, sha256-verified), `GET /eval/summary`, `timings_ms`, run metadata in RunState, `check.description` from rules.yaml `summary`, `fields_passed/fields_total`. 377 tests green. Details: NOTAS §22, docs/API.md §2.4–2.4d. Redeploy needs Carlos's OK.
**19-oct (later):** rule-learning loop committed on `feat/kb-extractor` (NOTAS §21), 277 tests green, NOT pushed and NOT deployed. `origin/main` has Jose's 2da7cc0 (typed document endpoints) touching api.py/api_models.py/openapi.json: expect conflicts on merge (regenerate openapi.json).

---

## 1. Goal

### 1.1 The KB Track 6 brief (original, verbatim — pasted by Carlos 2026-10-19)
**Context:** back-office teams check documents by hand, compare data across supporting materials
and chase missing or ambiguous information. Build a generally applicable solution that speeds up
the check, makes it more consistent and produces a traceable control protocol. The AI output
supports the decision; it is not an automatic final approval.

**Expected prototype:**
- Accept a package of documents for one business case.
- Check the set is complete: about 5-7 different documents, always defined in the "Contract"
  document. Classify each document, then extract the pre-defined attributes for its type, with a
  confidence level.
- If something is missing or specific inconsistencies appear, automatically trigger a request to
  the client to supplement or correct.
- Sort results into passed checks, discrepancies, warnings and manual review, and generate a
  cited, auditable protocol with a recommended next step for the back-office worker.

**Technical tasks:**
- Ingestion and preprocessing: load PDFs and images, OCR, keep the link to the original page.
- Classification: determine the document type. Bonus: split multi-document PDFs and handle
  shuffled pages.
- Extraction: structured attributes with source citation and confidence score.
- Rules engine: configurable checks for completeness, consistency and logical links.
- Human in the loop: confirm, edit, reject or send back for supplementation.
- Audit trail: input versions, applied rule or model, run timestamp, history of human
  interventions.

**Demo scenario:** a happy path (a complete set passes and a protocol is generated), an error path
(a document is missing, or a key data item is missing or doesn't match), and the automatic
resolution or human next steps.

**Evaluated on:** accuracy of classification, extraction and checks; traceability of evidence and
explainability; robustness to document quality and variants; real-world usability for the back
office.

**Guardrails:** rules must be separate from extraction and easy to change; the rules should
improve continuously from real decisions.

**Data:** No documents are provided. KB does not share any documents for this challenge. Create
your own synthetic document packages (a Contract that defines the 5-7 required documents, plus the
supporting documents, incl. scans, missing pages and inconsistent data for the error path). No
real client data.

### 1.2 Product decisions from Carlos
- Market: **Czech** (Czech document types, rodné číslo rules, KB context, Czech names/addresses in
  synthetic data). Admin view and protocol UI may be **English**. Client drafts **Czech + English**.
- Each package: a Contract listing **5–7 DIFFERENT required types**.
- Code freeze: **Tue 2026-10-20 13:30 Europe/Prague**.

## 2. Team
| Who | Owns |
|---|---|
| **Carlos** (`aisona-lab`) | this engine/backend: extraction, rules, audit, API, AWS deploy |
| **Jose** | frontend + Main Sequence integration/scaffolds. Repos: `mainsequence-projects/ParsingMotorBackend` (this one) and `mainsequence-projects/Parsing-Motor-Frontend`. Carlos has **no push** to the frontend repo yet. |

## 3. Hard rules (do not break)
1. Work on local branch **`feat/kb-extractor`**. **No `git push`, no PR** until Carlos says so.
2. Commits authored as **`Carlos <233920722+aisona-lab@users.noreply.github.com>`** (already in local
   git config). One logical change per commit, **Spanish body explaining WHY**, plus a regression test.
3. AWS region **eu-central-1 only** (code refuses other regions: `extractor/config.py`).
4. Budget: account max **800 USD**; this build **≤ 20 USD**; spend nothing without telling Carlos.
   Use the cost meter (`EXTRACTOR_COST_LEDGER`, `EXTRACTOR_BUDGET_USD`) and the cache (`EXTRACTOR_CACHE=1`).
5. **Synthetic data only.** No real personal data, ever.
6. **Never commit or print secrets.** The demo token lives only in `.deploy_token` (gitignored,
   chmod 600) and in the Lambda env `EXTRACTOR_API_KEY`. `deploy.py` reuses it; never rotate it silently.
7. Do not create new AWS resource *types* without asking (updating the existing Lambda is fine).
8. If AWS answers `ExpiredToken`: stop and ask Carlos to refresh SSO.

## 4. Architecture
```
package (PDF/JPG/PNG, 1..12 files, ≤20 pages)
  │ ingest.py  magic-byte sniff, limits, render pages @150 dpi, sha256 per file/page
  ▼
classify.py   Bedrock Claude Haiku 4.5 per page → doc_type (7 Czech types + other), page x/y marker,
  ▼           doc_key, rotation
splitter.py   deterministic grouping (file, type, doc_key, page numbers) → logical documents
  ▼           (shuffled pages; 'page 2 of 2' never starts a doc; unnumbered scan pages fill gaps;
              missing pages from markers)
extract.py    per document, IN PARALLEL:
              ├─ Textract AnalyzeDocument QUERIES (value + confidence + page + bbox, + OCR lines)
              │    + `textract: lines_rule` fields: deterministic regex on OCR text (tax arrears)
              └─ Bedrock Claude Sonnet 4.5 Converse (JSON, value + page; gets the same question)
  ▼
reconcile.py  CODE, not AI: normalize both values, compare (exact / numeric tol / similarity),
              pick value + status per field (rules.yaml FIELD_RECONCILE). Textract has NO Czech
              model: if the only difference is dropped accented letters → agree_ocr_no_diacritics,
              keep the LLM spelling (normalize.ocr_dropped_diacritics)
  ▼
cross_checks.py + rules.yaml   document checks (rodné číslo, dates, TAX_CLEAR), package checks
              (names, RČ, address, income incl. employer confirmation, XDOC_EMPLOYER, DOC_RECENT
              ≤30 days configurable, XDOC_HOLDER_RC certificate holder = ID, completeness vs
              Contract, page continuity, unexpected docs)
  ▼
evaluate.py   next step policy + client draft (cs + en);  audit.py  hash-chained JSONL per run
protocol.py   cited control protocol HTML/PDF rebuilt from the audit log
  ▼
api.py (FastAPI/Mangum on Lambda) → JSON for Jose's frontend; HITL decisions re-evaluate offline
```
Golden rule: **AI extracts, code decides.** The LLM never picks the final value or status.
Diagram: `arquitectura_motor.png` (source `/workspace/hack/diagram/arch.py`).

### 4.1 Module map
| File | One line |
|---|---|
| `extractor/config.py` | region lock (Frankfurt), model ids, loads `schemas.yaml` → `SCHEMAS`, `DOC_TYPES` |
| `extractor/schemas.yaml` | per doc type: fields, field type, Textract query |
| `extractor/rules.yaml` | ALL thresholds and rules (versioned by sha256[:16]); next-step policy |
| `extractor/rules_engine.py` | loads/validates rules.yaml, `rules.check(...)` builds cited check dicts |
| `extractor/ingest.py` | input validation (`InputError` → HTTP 4xx), rendering, limits, safe names |
| `extractor/aws.py` | boto3 Textract/Bedrock wrappers, retries, content cache, cost metering |
| `extractor/cache.py` | content-addressed cache in `.cache/aws` (on when `EXTRACTOR_CACHE=1`) |
| `extractor/cost.py` | cost ledger + budget guard (`BudgetExceeded` → HTTP 503) |
| `extractor/classify.py` | per-page classification prompt (Haiku) |
| `extractor/splitter.py` | page grouping into documents |
| `extractor/extract.py` | dual extraction per document (Textract ‖ Bedrock) |
| `extractor/normalize.py` | strict normalizers: amount, date (CZ months), name, RČ, IBAN, address |
| `extractor/reconcile.py` | field-level reconciliation → value/source/page/bbox/confidence/status |
| `extractor/doclist.py` | deterministic parser of the Contract's "required documents" list (2nd reader) |
| `extractor/rules.py` | rodné číslo parser/validator |
| `extractor/cross_checks.py` | document + package checks, completeness, next step, client draft |
| `extractor/evaluate.py` | pure evaluation (no AWS) incl. human overrides |
| `extractor/pipeline.py` | orchestrates a run, writes audit events, returns the API JSON |
| `extractor/audit.py` | hash-chained JSONL per run, decisions, verify, learning report |
| `extractor/rule_changes.py` | rule-learning loop: suggestions → one-value rules.yaml patch + offline impact → human approve/reject (hash-chained `rules_ledger.jsonl`) → new rules file, never auto-activated |
| `extractor/api.py` | FastAPI endpoints + demo HTML; `api_models.py` = response models |
| `extractor/protocol.py` | control protocol (HTML; PDF via PyMuPDF Story) from the audit log |
| `extractor/cli.py` | `python -m extractor ...` (run, recheck, decide, verify, learn, protocol) |
| `scripts/gen_packages.py` | the 12 Czech synthetic packages (12 people, valid RČ, 2–3 layouts per type, 5 fonts, scan/photo degradations; truth from inputs) |
| `scripts/md_to_docx.py` | Markdown → .docx for the spec (PDF via `soffice --headless --convert-to pdf`) |
| `scripts/eval.py` | scores every package vs `ground_truth.json` → `samples/out/eval_summary.json` |
| `scripts/export_openapi.py` | writes `docs/openapi.json` (a test fails if stale) |
| `deploy/deploy.sh` / `deploy.py` / `deploy_cloudfront.py` | build zip, update Lambda (env preserved), CloudFront |
| `deploy/signed_extract.py` | CLI: `/session` → SigV4 POST to the Function URL |
| `deploy/teardown.sh` | removes the deployed resources |
| `api/kb_extractor/main.py` | Main Sequence entry point (same FastAPI app) |
| `tests/` | 277 tests, no AWS (`tests/fixtures` hold recorded runs) |
| `docs/API.md`, `docs/openapi.json` | HTTP contract for Jose (ES+EN) |
| `NOTAS_CARLOS.md` | Spanish decision log: every action and why |

### 4.2 Key JSON (full schemas: `docs/openapi.json`, models: `extractor/api_models.py`)
```jsonc
// POST /extract → ExtractResponse (abridged)
{"run_id":"20261019T125642Z-161958ed","rules_version":"4932db49748e6d14","engine_version":"0.4.0",
 "models":{"classification":"eu.anthropic.claude-haiku-4-5-…","extraction":"eu.anthropic.claude-sonnet-4-5-…"},
 "inputs":[{"name":"package.pdf","bytes":12345,"sha256":"…"}],
 "documents":[{"doc_id":"d1","doc_type":"contract","pages":[{"source_file":"package.pdf","page":1}],
   "fields":{"declared_monthly_income":{"value":"52000.00 CZK","normalized":52000.0,
     "textract":"52000.00 CZK","textract_conf":0.79,"bedrock":52000.0,"source":"both",
     "page":1,"bbox":{…},"outcome":"agree_exact","status":"passed","rule_ids":["FIELD_RECONCILE","XDOC_INCOME"]}},
   "checks":[…]}],
 "checks":[{"check_id":"XDOC_INCOME:income_contract_vs_payslip:package","rule_id":"XDOC_INCOME",
   "status":"discrepancy","detail":"52000.0 vs 38000.0 (diff=26.92%, tol=1%)","evidence":[{"doc_id":"d1","field":"declared_monthly_income","value":"…","page":1}]}],
 "required_documents":["contract","id_card",…],"missing_documents":[],"missing_pages":[],
 "unexpected_documents":[],"recommended_next_step":"manual_review_discrepancy",
 "draft_client_message":"Dear …","draft_client_messages":{"cs":"Vážená/ý …","en":"Dear …"}}
// GET /runs/{id}/protocol?format=html|pdf → cited protocol (CLI: python -m extractor protocol <run_id> -o p.pdf)
// POST /runs/{id}/decisions  {"field":"contract.declared_monthly_income","action":"edit","new_value":"38000","reviewer":"jose"}
// GET /rules/proposals → one-value rules.yaml patches + impact (less_cautious_runs!)
// POST /rules/proposals/{id}/review {"action":"approve","reviewer":"lead_ana"} → new rules version (activate manually)
// CLI: python -m extractor rules propose | rules review <id> --action approve --reviewer ana --write extractor/rules.yaml
```
Statuses: field `passed|warning|manual_review|missing`; check `passed|warning|discrepancy|manual_review`.
Next steps: `manual_review_discrepancy`, `request_missing_documents`, `manual_review_extraction`,
`spot_check_warnings`, `approve_ready` (order in `rules.yaml → next_step`).

## 5. How to run
```bash
cd /workspace/hack/ParsingMotorBackend
python -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
# (on Carlos's box: tests use /tmp/kb-venv, deploy uses /workspace/hack/venv)
python -m pytest -q                                  # no AWS
python -m extractor samples/01_*/package.pdf -o /tmp/o.json --cache
python -m extractor verify <run_id>; python -m extractor recheck <run_id> --rules x.yaml
python -m extractor protocol <run_id> -o /tmp/protocol.pdf
python scripts/gen_packages.py                       # regenerates samples/ (PDF bytes change → new cache keys)
uvicorn extractor.api:app --port 8000                # Swagger at /docs
python scripts/eval.py --cache                       # all packages; drop --cache for a real re-run
python scripts/export_openapi.py                     # after any API change
bash deploy/deploy.sh                                # update the EXISTING Lambda + CloudFront (clean tree!)
bash deploy/teardown.sh                              # remove everything (ask Carlos first)
```
Env var **names** (values never in git): `AWS_REGION` (=eu-central-1), AWS SSO creds,
`EXTRACTOR_CACHE`, `EXTRACTOR_COST_LEDGER`, `EXTRACTOR_BUDGET_USD`, `EXTRACTOR_RULES`,
`EXTRACTOR_AUDIT_DIR`, `EXTRACTOR_API_KEY`, `EXTRACTOR_CORS_ORIGINS`, `EXTRACTOR_MAX_FILES`,
`EXTRACTOR_MAX_PAGES`, `EXTRACTOR_MAX_FILE_MB`, `EXTRACTOR_GIT_COMMIT`, `BEDROCK_MODEL`,
`BEDROCK_CLASSIFY_MODEL`, `BEDROCK_FALLBACK_MODELS`, `DEMO_INVOKE_ROLE_ARN`, `FUNCTION_URL`,
`RENDER_DPI`, `MAX_WORKERS`.

## 6. Deployed resources (eu-central-1, account of the hackathon team, SSO)
| Resource | Name |
|---|---|
| Lambda | `fintechers-kb-extractor` (python3.12, 1024 MB, 180 s) |
| Function URL | `https://mokpk7khaf3azp3fhb3h4ruiqm0gflxj.lambda-url.eu-central-1.on.aws/` — **AuthType AWS_IAM** |
| CloudFront | `https://d31su4idgeitei.cloudfront.net/` (dist `E13GPX9M1FZKBI`, OAC `fintechers-kb-extractor-oac`, ORP `fintechers-kb-extractor-orp`) |
| IAM exec role | `fintechers-kb-extractor-role` (Textract, Bedrock eu.*, Logs, AssumeRole demo) |
| IAM demo role | `fintechers-kb-extractor-demo-invoke` (only InvokeFunctionUrl, 15-min sessions) |
| Logs | `/aws/lambda/fintechers-kb-extractor` (7 days) |

**Auth flow:** header `x-api-key` (token from `.deploy_token`) on every endpoint except `/health`.
GETs go through CloudFront (OAC signs). POST bodies can't be signed by OAC, so: `GET /session`
→ 15-min STS creds for the demo role → browser/CLI SigV4-signs `POST {functionUrl}extract_json`
(service `lambda`). See `docs/API.md` §1 and `deploy/signed_extract.py`.

## 7. Current numbers (measured, not estimated)
**v0.4.0, 12 Czech packages, full run WITHOUT cache on real AWS** (`samples/out/eval_summary.json`,
log `samples/out/eval_nocache_run.log`). The cached run gave identical numbers (temperature 0).

| Package | Types | Fields | Checks | Seeded flags | False alarms | Next step (expected) |
|---|---|---|---|---|---|---|
| 01_happy_all7_A | 7 | 48/48 | 32/32 | 0/0 | 0 | spot_check_warnings (approve_ready) |
| 02_happy_all7_B | 7 | 47/48 | 31/32 | 0/0 | 1 | spot_check_warnings (approve_ready) |
| 03_missing_tax_certificate | 6 of 7 | 40/42 | 26/28 | 1/1 | 1 | request_missing_documents ✓ |
| 04_missing_statement_page | 6 | 43/44 | 30/31 | 1/1 | 1 | request_missing_documents ✓ |
| 05_income_mismatch_employer | 7 | 48/48 | 32/32 | 2/2 | 0 | manual_review_discrepancy ✓ |
| 06_employer_mismatch_stale_cert | 7 | 46/48 | 31/32 | 2/2 | 1 | manual_review_discrepancy ✓ |
| 07_invalid_rc_name_typo | 6 | 42/42 | 24/27 | 3/3 | 0 | manual_review_discrepancy ✓ |
| 08_scanned_noisy_5types | 5 | 38/38 | 27/27 | 0/0 | 0 | spot_check_warnings (approve_ready) |
| 09_shuffled_6types | 6 | 42/44 | 29/31 | 0/0 | 2 | manual_review_extraction (approve_ready) |
| 10_photo_id_tax_arrears | 7 | 47/48 | 31/32 | 1/1 | 1 | manual_review_discrepancy ✓ |
| 11_extra_doc_cert_other_person | 7 + other | 44/44 | 31/31 | 3/3 | 0 | manual_review_discrepancy ✓ |
| 12_happy_5types_B | 5 | 33/33 | 23/23 | 0/0 | 0 | spot_check_warnings (approve_ready) |

Overall: page classification **100 %**, grouping **12/12**, fields **518/527 = 98.3 %**, checks
**347/358 = 96.9 %**, seeded problems **13/13**, false alarms 7 (6/12 packages clean), next step
**7/12 exact, 12/12 safe** (every miss is MORE cautious), missing docs/pages/unexpected 12/12.
Before the v0.4 fixes the first full run scored fields 97.0 %, checks 94.7 %, next step 5/12, one
UNSAFE miss (tax arrears read as "none" by both engines) — fixed.
Remaining false alarms: employer-confirmation issue date in Czech prose letters (Textract picks the
employment-start date → engines disagree → manual review, never "stale"); one 'Č'→'A' OCR misread.

Cost: uncached run 2.30 USD for 85 pages ≈ **0.027 USD/page, ≈ 0.19 USD per package** (0.215 for
an 8-page 7-type package). Lambda: 5-page package ≈ 15 s. Spend this task **5.74 USD**; cumulative
**≈ 9.10 USD** of the 20 USD build cap (ledger `.cost/task2_2026-10-19.json`, gitignored).

## 8. Status vs the brief (summary; full matrix in `docs/MAIN_GOAL_SPEC.md` §2)
| Area | Status |
|---|---|
| Ingest + OCR + page link | Done |
| Classification + split/shuffle | Done |
| Extraction with citation + confidence (dual engine) | Done |
| Configurable rules engine (rules.yaml, versioned) | Done |
| Packages with 5–7 different types per Contract (7 Czech types) | Done (v0.4) |
| Cited control protocol HTML/PDF (+ CLI) | Done (v0.4, deployed) |
| Czech + English client draft | Done (v0.4) |
| HITL confirm/edit/reject/request_documents (API) | Done (UI = Jose) |
| Audit trail (hashes, versions, timestamps, human history, verify) | Partial: Lambda storage ephemeral |
| Learning loop from decisions | Done locally (proposal → impact → review → new version; activation is manual). Not deployed |
| Back-office UI | Jose (frontend repo) |
| Durable audit on Lambda | Missing: needs S3/DynamoDB = new resource type → **Carlos must approve** |

## 9. Exact next steps (in order)
0. (v0.5.0, local) With Carlos's OK: push `feat/engine-mockup-parity`, PR to main, redeploy the SAME
   Lambda with `deploy.sh` on a clean tree (bundles `eval_summary.json`). Frontend widgets map:
   queue → `GET /runs` (+ `/eval/summary` KPI cards); review screen → `GET /runs/{id}` (fields,
   bbox, checks+description, drafts, timings, inputs/pages) + `/pages/{n}.png` + `/events` +
   `/verify`; actions → `POST /decisions`; protocol → `/protocol?format=pdf`.
1. Jose / Main Sequence: wire `/extract_json`, `/runs/{id}`, `/runs/{id}/decisions`,
   `/runs/{id}/protocol` (open the protocol right after the extract: audit is per container).
2. Carlos decides on durable audit (S3 bucket or DynamoDB table). Do NOT create without approval.
3. Optional engine work: issue date in Czech prose letters (OCR line rule like `lines_rule`
   for "V <city> dne <date>"), happy paths reaching `approve_ready` (warnings come from
   low-confidence Textract on Czech text / llm_only fields).
4. Rehearse the demo (`docs/MAIN_GOAL_SPEC.md` §6); freeze rules.yaml before 13:30 Tue.
5. Push only when Carlos says so.

## 10. Known limits and gotchas
- **AWS SSO creds expire** (hours). Symptom `ExpiredToken`: stop, ask Carlos to re-login.
- **Function URL is AWS_IAM** because the org (SCP) blocks anonymous Function URL access (403 even with `Principal:*`).
- **Reserved concurrency cannot be set**: account has 10 concurrent executions total; AWS needs ≥10 unreserved.
- **Audit on Lambda lives in `/tmp`**: per container, lost on cold start → `/runs/{id}` can 404. Budget guard also per container. Since 0.5.0 the same applies to `GET /runs`, `/events` and `/pages/{n}.png` (kept input bytes = personal data, same retention as the audit; `EXTRACTOR_KEEP_INPUTS=0` disables).
- `check.description` comes from the run's own rules (audit), so pre-0.5 runs show `description.en`, not the new `summary`.
- `fields_auto_validated_pct` = passed + warning (compat). The "39/48 passed" KPI is `fields_passed/fields_total`.
- **`eu.*` Bedrock inference profiles** may process in other EU regions (data stays in the EU, not only Frankfurt).
- **The Main Sequence wheel is missing the server part** (Jose's scaffold); run the API with uvicorn/Lambda meanwhile.
- CloudFront origin read timeout is 60 s (that's why POST goes directly to the Function URL; Lambda allows 180 s).
- Function URL request body ≤ 6 MB (base64 ⇒ ~4.4 MB PDF).
- Lambda CORS comes from the Function URL config; **never** set `EXTRACTOR_CORS_ORIGINS` on Lambda (duplicate headers).
- Only first 6 pages of a document go to Bedrock; two same-type docs without a distinguishing key are merged.
- Hash chain is not anchored externally.
- `deploy.sh` must run on a clean tree so `EXTRACTOR_GIT_COMMIT` is exact.
- **Textract has no Czech model**: drops ř/č/š/ž/ů/í ('Ondřej'→'Ondej', 'Kč'→'K', 'října'→'rjna')
  and cannot reason over Czech prose (letters). Handled deterministically (diacritic-tolerant
  agreement, OCR month variants, amount with lost decimal comma, `lines_rule`); don't "fix" by
  tuning ground truth.
- Regenerating samples changes PDF bytes (reportlab timestamps) → AWS cache misses → real spend.
- A Bedrock prompt change invalidates the whole Bedrock cache (key includes the prompt).

## 11. Where things are
Repo `/workspace/hack/ParsingMotorBackend` (Carlos's box). Zip for sharing: `/workspace/hack/extractor.zip`.
Old prototype repo `/workspace/hack/extractor` (superseded). Spec: `docs/MAIN_GOAL_SPEC.md` (+ .docx/.pdf).

## 12. Ready-to-paste first prompt for Claude
```
You are continuing Carlos's KB Track 6 engine. Read HANDOFF_CLAUDE.md fully, then NOTAS_CARLOS.md
(latest sections) and docs/MAIN_GOAL_SPEC.md. Rules: stay on local branch feat/kb-extractor, never
push or open a PR, commit as Carlos <233920722+aisona-lab@users.noreply.github.com> with a Spanish
body explaining WHY, one change per commit with a regression test; AWS eu-central-1 only;
synthetic data only; never print or commit the token in .deploy_token; tell Carlos before any AWS
spend and keep EXTRACTOR_COST_LEDGER/EXTRACTOR_BUDGET_USD set. First run `python -m pytest -q`
and `git log --oneline | head -20`, then continue with the first unchecked item in section 9.
Update HANDOFF_CLAUDE.md and NOTAS_CARLOS.md after each step.
```
