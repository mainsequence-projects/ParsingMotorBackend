"""FastAPI surface for Jose / Main Sequence integration."""
from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.responses import JSONResponse

from . import __version__
from .pipeline import run_uploads

app = FastAPI(title="KB Document Control Extractor", version=__version__,
              description="Dual Textract+Bedrock extraction with deterministic reconciliation.")


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@app.post("/extract")
async def extract(files: list[UploadFile] = File(...),
                  explain: bool = Query(False, description="LLM explanation of disagreements")):
    if not files:
        raise HTTPException(400, "upload at least one PDF/PNG/JPG")
    payload = []
    for f in files:
        data = await f.read()
        if not data:
            raise HTTPException(400, f"empty file: {f.filename}")
        payload.append((f.filename or "upload.bin", data))
    try:
        result = run_uploads(payload, explain=explain)
    except Exception as e:
        raise HTTPException(500, f"extraction failed: {e}") from e
    return JSONResponse(result)
