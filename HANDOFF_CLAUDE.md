# HANDOFF — KB Document Control Engine (Track 6)

> **Resumen (ES).** Motor de control documental back-office para Komerční banka (mercado checo).
> Rama local `feat/kb-extractor`, **sin push** hasta que Carlos lo diga. Commits como Carlos con
> cuerpo en español explicando el *porqué*. Solo `eu-central-1`, solo datos sintéticos, nunca
> secretos en git (el token de la demo vive solo en `.deploy_token`). Presupuesto: 800 USD cuenta,
> tope 20 USD para el build. Code freeze **mar 20-oct-2026 13:30 (Praga)**. Este archivo es para
> que otra IA (Claude) continúe sin contexto: lee las secciones 4, 9, 10 y 12 primero.

Last updated: 2026-10-19 (Europe/Prague). Keep this file updated after every commit.

---

## 1. Goal

### 1.1 The KB Track 6 brief (reconstructed)
The original brief text was given to Carlos in chat and is **not stored in the repo**. Below is the
full requirement list as it was handed to the engineering agent, item by item. If Carlos pastes
the original, replace this subsection verbatim.

**Question:** *How can AI make the back-office control of loan/KYC document packages faster and
more reliable, while every decision stays explainable and auditable?* A back-office clerk receives
a **case package**: a **Contract** that defines which supporting documents are required, plus
**5–7 different document types** (Czech market: ID card / občanský průkaz, bank statement / výpis
z účtu, payslip / výplatní lístek, proof of address, employer confirmation / potvrzení o
zaměstnání, tax-office no-debt certificate / potvrzení o bezdlužnosti, …). Pages can arrive as
scans, photos, shuffled, with missing pages, or with inconsistent data.

**Expected prototype**
- Ingest a package, classify every page, split/group it into documents.
- Extract key fields per document type **with citation** (document, page, location) and
  **confidence**.
- Run a **configurable rules engine**: completeness vs the Contract, consistency across documents
  (names, birth number/rodné číslo, address, income, employer), logical links (dates, validity).
- Produce a **recommended next step** and, when something is missing/wrong, a **draft request to
  the client**.
- **Human in the loop**: confirm / edit / reject / send back (request documents).
- **Audit trail**: input versions (hashes), rule and model versions, timestamps, human decision
  history.
- A **control protocol** the back-office can sign (cited HTML/PDF).

**Technical tasks:** ingestion + OCR with link to the original page; classification (+ bonus:
split/shuffle); extraction with citation and confidence; configurable rules engine; HITL
confirm/edit/reject/send-back; audit trail (input versions, rule/model, timestamp, human history).

**Demo scenario:** happy path → error path (missing/inconsistent → client request drafted) →
resolution (human edit/confirm → re-evaluated → approve_ready, audit verifies).

**Evaluation criteria:** accuracy; traceability/explainability; robustness; back-office usability.

**Guardrails:** rules separate from extraction and easy to change; rules can improve from real
human decisions (suggestions, never silently auto-applied).

**Data:** synthetic packages only (5–7 docs each, scans, missing pages, inconsistent data).

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
classify.py   Bedrock Claude Haiku 4.5 per page → doc_type, page x/y marker, doc_key, rotation
  ▼
splitter.py   deterministic grouping (file, type, doc_key, page numbers) → logical documents
  ▼                         (works for shuffled pages; detects missing pages from markers)
extract.py    per document, IN PARALLEL:
              ├─ Textract AnalyzeDocument QUERIES (value + confidence + page + bbox, + OCR lines)
              └─ Bedrock Claude Sonnet 4.5 Converse (JSON, value + page)
  ▼
reconcile.py  CODE, not AI: normalize both values, compare (exact / numeric tol / similarity),
              pick value + status per field (rules.yaml FIELD_RECONCILE)
  ▼
cross_checks.py + rules.yaml   document checks (rodné číslo, dates), package checks (names,
              RČ, address, income, completeness vs Contract, page continuity, unexpected docs)
  ▼
evaluate.py   next step policy + client draft;  audit.py  hash-chained JSONL per run
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
| `extractor/api.py` | FastAPI endpoints + demo HTML; `api_models.py` = response models |
| `extractor/cli.py` | `python -m extractor ...` (run, recheck, decide, verify, learn) |
| `scripts/gen_samples.py`, `scripts/gen_hard_samples.py` | synthetic package generators (truth from inputs) |
| `scripts/eval.py` | scores every package vs `ground_truth.json` → `samples/out/eval_summary.json` |
| `scripts/export_openapi.py` | writes `docs/openapi.json` (a test fails if stale) |
| `deploy/deploy.sh` / `deploy.py` / `deploy_cloudfront.py` | build zip, update Lambda (env preserved), CloudFront |
| `deploy/signed_extract.py` | CLI: `/session` → SigV4 POST to the Function URL |
| `deploy/teardown.sh` | removes the deployed resources |
| `api/kb_extractor/main.py` | Main Sequence entry point (same FastAPI app) |
| `tests/` | ~157 tests, no AWS (`tests/fixtures` hold recorded runs) |
| `docs/API.md`, `docs/openapi.json` | HTTP contract for Jose (ES+EN) |
| `NOTAS_CARLOS.md` | Spanish decision log: every action and why |

### 4.2 Key JSON (full schemas: `docs/openapi.json`, models: `extractor/api_models.py`)
```jsonc
// POST /extract → ExtractResponse (abridged)
{"run_id":"20261019T125642Z-161958ed","rules_version":"4932db49748e6d14","engine_version":"0.3.0",
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
 "draft_client_message":"Dear …"}
// POST /runs/{id}/decisions  {"field":"contract.declared_monthly_income","action":"edit","new_value":"38000","reviewer":"jose"}
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
v0.3.0 eval, 9 old packages (4–5 doc types each, single template): page classification 1.0,
grouping 9/9, fields 255/255, expected flags 8/8, false alarms 0, next step 7/9 (9/9 safe).
Lambda: 5-page package 13 s, 0.13 USD uncached. **Cumulative AWS spend ≈ 3.36 USD.**
(Will be replaced by the new 5–7-type package eval — see NOTAS_CARLOS.md latest section.)

## 8. Status vs the brief (summary; full matrix in `docs/MAIN_GOAL_SPEC.md` §2)
| Area | Status |
|---|---|
| Ingest + OCR + page link | Done |
| Classification + split/shuffle | Done |
| Extraction with citation + confidence (dual engine) | Done |
| Configurable rules engine (rules.yaml, versioned) | Done |
| HITL confirm/edit/reject/request_documents (API) | Done (UI = Jose) |
| Audit trail (hashes, versions, timestamps, human history, verify) | Done (Lambda storage ephemeral) |
| Packages with 5–7 different types per Contract | **In progress** (employer_confirmation, tax_or_debt_certificate) |
| Cited control protocol HTML/PDF | **In progress** |
| Czech + English client draft | **In progress** |
| Learning loop from decisions | Partial (report + suggestions, never auto-applied) |
| Back-office UI | Jose (frontend repo) |
| Durable audit on Lambda | Missing (needs S3/DynamoDB = new resource type → ask Carlos) |

## 9. Exact next steps (in order)
1. Add doc types `employer_confirmation`, `tax_or_debt_certificate` (schemas.yaml, classifier
   hints, rules: employer/income vs payslip, certificate ≤30 days, holder = ID).
2. Regenerate ALL synthetic packages: Czech persons, ≥2 layouts per type, different fonts,
   Contracts requiring 5–7 types; happy path (7), missing doc, missing page, inconsistent value,
   scanned, shuffled, one Contract requiring only 5. Truth from generator inputs only.
3. `GET /runs/{id}/protocol` (HTML + PDF) + CLI `protocol`.
4. Czech + English client draft.
5. Full eval on real AWS (cache, then one uncached re-run), update NOTAS, API.md, openapi.json.
6. Redeploy same Lambda, verify `/health`, protocol, one extract via CloudFront; rebuild
   `/workspace/hack/extractor.zip` (no secrets/.cache/.cost/audit).
7. Hand Jose the contract (`docs/API.md`) and demo script (`docs/MAIN_GOAL_SPEC.md` §6).

## 10. Known limits and gotchas
- **AWS SSO creds expire** (hours). Symptom `ExpiredToken`: stop, ask Carlos to re-login.
- **Function URL is AWS_IAM** because the org (SCP) blocks anonymous Function URL access (403 even with `Principal:*`).
- **Reserved concurrency cannot be set**: account has 10 concurrent executions total; AWS needs ≥10 unreserved.
- **Audit on Lambda lives in `/tmp`**: per container, lost on cold start → `/runs/{id}` can 404. Budget guard also per container.
- **`eu.*` Bedrock inference profiles** may process in other EU regions (data stays in the EU, not only Frankfurt).
- **The Main Sequence wheel is missing the server part** (Jose's scaffold); run the API with uvicorn/Lambda meanwhile.
- CloudFront origin read timeout is 60 s (that's why POST goes directly to the Function URL; Lambda allows 180 s).
- Function URL request body ≤ 6 MB (base64 ⇒ ~4.4 MB PDF).
- Lambda CORS comes from the Function URL config; **never** set `EXTRACTOR_CORS_ORIGINS` on Lambda (duplicate headers).
- Only first 6 pages of a document go to Bedrock; two same-type docs without a distinguishing key are merged.
- Hash chain is not anchored externally.
- `deploy.sh` must run on a clean tree so `EXTRACTOR_GIT_COMMIT` is exact.

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
