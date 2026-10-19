# KB Document Control Extractor

Dual-engine document extraction for the FinTechers Hackathon — Komerční banka challenge
**“Automating back-office document controls”** (Track 6).

Carlos owns the extraction engine. Jose integrates it into Main Sequence via the HTTP API.

## What it does

1. Ingest PDF / PNG / JPG (multi-page OK; each value keeps its page number).
2. Classify each page: `contract | id_card | bank_statement | payslip | proof_of_address | other`.
3. Split a multi-document PDF into contiguous same-type groups.
4. Extract per-type fields (see `extractor/schemas.yaml`) with **Textract QUERIES** and **Bedrock Converse** in parallel.
5. **Deterministic reconciler** (Python, no LLM decides): normalize → compare → status
   `passed | warning | manual_review | missing`.
6. Deterministic rules: Czech *rodné číslo*, birth-date consistency, cross-document name/income,
   completeness vs the contract’s required-documents list.
7. Output JSON with checks, missing docs, recommended next step, and a draft English client email.

## Architecture (LLM vs deterministic)

| Step | Who decides |
|------|-------------|
| Classify page | Bedrock (temperature 0) |
| Extract field candidates | Textract + Bedrock (parallel) |
| Choose / score field | **Code** (`reconcile.py`) |
| Birth-number / cross-doc / completeness | **Code** (`rules.py`, `cross_checks.py`) |
| Optional disagreement explanation | Bedrock (one sentence, never picks the value) |

## Region (Frankfurt)

- Every boto3 client is created with `region_name='eu-central-1'`.
- If `AWS_REGION` / `AWS_DEFAULT_REGION` is set to anything else, the process **exits**.
- On-demand model IDs like `anthropic.claude-sonnet-4-5-...` are **not** invokable in
  eu-central-1 (ValidationException — inference profile required).
- Default model: `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`.
  The client call is made **from Frankfurt**; Bedrock Cross-Region Inference for `eu.*`
  profiles may process the request in other **EU** regions. Data stays in the EU, not
  necessarily only in Frankfurt. See `NOTAS_CARLOS.md`.

## Setup

```bash
cd /workspace/hack/extractor
source ../venv/bin/activate   # or: python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# AWS SSO temp credentials already exported:
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
# Region locked to eu-central-1
```

## CLI

```bash
python -m extractor samples/01_clean/package.pdf -o /tmp/out.json
python -m extractor samples/02_missing_payslip/   # folder of files
python -m extractor samples/01_clean/package.pdf --explain  # LLM explanation of disagreements
```

## Deployed demo URL

Public (CloudFront): see `deploy/public_url.txt`  
API token: `.deploy_token` (gitignored)  
Deploy: `bash deploy/deploy.sh` · Teardown: `bash deploy/teardown.sh`  
Signed CLI extract: `python deploy/signed_extract.py samples/02_missing_payslip/id_card.pdf`

> Org blocks anonymous Function URLs (AuthType NONE → 403). Public GETs go through
> CloudFront OAC; browser POSTs use short-lived STS creds from `GET /session`. Details in
> `NOTAS_CARLOS.md` §8.

## HTTP API (for Jose / Main Sequence)

```bash
uvicorn extractor.api:app --host 0.0.0.0 --port 8080
curl -s http://127.0.0.1:8080/health
curl -s -F "files=@samples/02_missing_payslip/package.pdf" \
     http://127.0.0.1:8080/extract | jq '.recommended_next_step, .missing_documents'
```

- `GET /health` → `{status, version}`
- `POST /extract` multipart `files` (one or more PDF/PNG/JPG) → full package JSON
- Query `?explain=true` adds one-sentence explanations on `manual_review` fields

## Synthetic samples

Under `samples/` (fake Czech person **Jana Nováková** only):

| Package | Intent |
|---------|--------|
| `01_clean` | All docs consistent |
| `02_missing_payslip` | Payslip absent |
| `03_amount_mismatch` | Declared income 52000 vs payslip/statement 38000 |
| `04_name_typo_invalid_rc` | Surname typo + invalid rodné číslo on ID |

Regenerate: `python scripts/gen_samples.py`

## Measured eval results (real AWS, 2026-10-19, Europe/Prague)

```
packages: 4
packages_fully_correct: 4 / 4
field_accuracy: 1.0
check_accuracy: 1.0
```

Per package (`fields_auto_validated_pct` = share of fields with status passed/warning):

| Package | next_step | auto % | missing |
|---------|-----------|--------|---------|
| 01_clean | spot_check_warnings | 97.0 | — |
| 02_missing_payslip | request_missing_documents | 96.3 | payslip |
| 03_amount_mismatch | manual_review_discrepancy | 97.0 | — |
| 04_name_typo_invalid_rc | manual_review_discrepancy | 93.9 | — |

Bedrock usage across the 4 package runs: **38 calls**, ~68.5k input tokens, ~3.7k output tokens.

Outputs: `samples/out/*.json`, summary `samples/out/eval_summary.json`.
Re-run: `python eval.py`

## Example JSON (abridged)

See `samples/out/example_snippet.json` and full `samples/out/01_clean.json`.

```json
{
  "recommended_next_step": "spot_check_warnings",
  "fields_auto_validated_pct": 97.0,
  "missing_documents": [],
  "documents": [
    {
      "doc_type": "contract",
      "fields": {
        "first_name": {"value": "Jana", "status": "passed", "confidence": 0.99, "page": 1},
        "birth_number": {"value": "885412/0000", "textract": "885412/0000",
                         "bedrock": "885412/0000", "status": "passed", "page": 1}
      }
    }
  ]
}
```

## Cost control (Carlos)

- Pay-per-call only. **No** provisioned throughput, endpoints, adapters, EC2, Lambda, API Gateway, OpenSearch, Knowledge Bases, or S3 buckets.
- Build cap: **5 USD** (`EXTRACTOR_BUDGET_USD`). Meter in `extractor/cost.py` aborts over cap. Ledger: `samples/out/cost_ledger.json`.
- Classify with **Haiku 4.5**; extract with **Sonnet 4.5**.
- Estimated total spend for this whole build: **~$2.45 USD**. Measured package 02: **$0.096**. See `NOTAS_CARLOS.md` § Coste.

## Limits / known quirks

- Textract QUERIES on synthetic PDFs can grab a nearby amount (loan vs income); reconciler
  prefers Bedrock for amounts unless Textract confidence ≥ 0.9.
- Czech space-thousands (`52 000`) confuse OCR — samples use `52000.00 CZK`.
- SSO credentials expire in hours; refresh from the team account portal.
- Haiku 4.5 returned `ServiceUnavailableException` during setup; Sonnet 4.5 `eu.*` works.
- No real customer data. Never commit AWS keys.

## Repo layout

```
extractor/          # Python package
  schemas.yaml      # per-type field schemas + Textract queries
  ingest.py         # PDF/image → page PNGs
  classify.py       # Bedrock page classifier + split
  extract.py        # dual Textract + Bedrock
  reconcile.py      # deterministic field merge
  rules.py          # rodné číslo
  cross_checks.py   # package-level rules + draft email
  pipeline.py       # orchestration
  api.py / cli.py
samples/            # synthetic packages + out/
scripts/gen_samples.py
eval.py
NOTAS_CARLOS.md     # Spanish decision log + Jose test guide
```
