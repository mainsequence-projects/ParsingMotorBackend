# parsingmotorbackend

## Quickstart

```bash
# from the repo root
pip install -e .
# or, if uv is available:
uv pip install -e .
```

The scaffold intentionally does not generate `requirements.txt` or `uv.lock`.
Dependency policy belongs to the code repository and can be added explicitly when needed.

## FastAPI application

The starter API is [api/app/main.py](api/app/main.py). Its location is part of
the Main Sequence discovery contract: deployable FastAPI applications use
`api/<name>/main.py` from the CodeRepository root. Keep the application under `api/` so
CodeRepository resource discovery can find it and associate it with the pushed Git
commit used by the CodeRepository image.

Run the starter locally from the CodeRepository root:

```bash
uvicorn api.app.main:app --host 127.0.0.1 --port 8001
```

---

## KB Extractor (FinTechers / Komerční banka)

Dual-engine back-office document control: **Textract + Bedrock**, deterministic reconciler,
Czech *rodné číslo* rules, cross-document checks. Built for the hackathon Track 6 challenge.

| Piece | Path |
|-------|------|
| Package | [`extractor/`](extractor/) |
| Main Sequence entry | [`api/kb_extractor/main.py`](api/kb_extractor/main.py) |
| Decision log (ES) | [`NOTAS_CARLOS.md`](NOTAS_CARLOS.md) |
| Architecture diagram | [`arquitectura_motor.png`](arquitectura_motor.png) |
| Synthetic samples + eval | [`samples/`](samples/), [`eval.py`](eval.py) |
| AWS deploy scripts | [`deploy/`](deploy/) |

### Run the KB API locally

```bash
# AWS SSO temp credentials in the environment (eu-central-1)
uvicorn api.kb_extractor.main:app --host 127.0.0.1 --port 8080
curl -s http://127.0.0.1:8080/health
```

CLI (same engine):

```bash
python -m extractor samples/02_missing_payslip/package.pdf -o /tmp/out.json
```

### Public demo

CloudFront (no token in this README): https://d31su4idgeitei.cloudfront.net/

API token is only on the deployer's machine (`.deploy_token`, gitignored). See `NOTAS_CARLOS.md` §8.

### Measured eval (AWS, 2026-10-19)

`4/4` packages fully correct · `field_accuracy=1.0` · `check_accuracy=1.0`
