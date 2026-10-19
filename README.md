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
