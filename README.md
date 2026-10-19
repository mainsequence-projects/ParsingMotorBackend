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

## Document extraction service

The parser owns classification, typed extraction and source evidence. Back Office owns
business requirements, cross-document checks, reviewer decisions and protocols through
one shared engine for loans, new accounts and other document sets.

The deployable extraction entry point remains `api/kb_extractor/main.py` for Main Sequence
resource discovery. Run it locally with:

```sh
uvicorn api.kb_extractor.main:app --host 127.0.0.1 --port 8080
python -m extractor classify classify-request.json -o classification.json
python -m extractor extract-typed extract-request.json -o extraction.json
```

`POST /classify_json` uses a caller-supplied type catalog. `POST /extract_typed_json` uses
an explicit type and complete canonical/draft JSON Schemas. See [API documentation](docs/API.md) and [OpenAPI](docs/openapi.json).

The old loan pipeline, policy files, review/audit/protocol routes and loan demo have been
removed. `/extract` and `/extract_json` are also removed: callers must use the two explicit
operations. This is a source-level breaking change; deployment is a separate action.

Historical hackathon notes, architecture images and synthetic samples remain for reference.
Their loan workflow and evaluation claims do not describe the current extraction service.

Run offline checks with `python -m pytest -q`. Regenerate the API contract after API changes
with `python scripts/export_openapi.py`. Provider calls require the configured AWS runtime;
offline tests do not establish live provider availability.
