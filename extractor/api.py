"""FastAPI surface for Jose / Main Sequence / demo. Lambda via Mangum."""
from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .pipeline import run_uploads

app = FastAPI(
    title="KB Document Control Extractor",
    version=__version__,
    description="Dual Textract+Bedrock extraction with deterministic reconciliation.",
)

API_KEY = os.getenv("EXTRACTOR_API_KEY", "")


def _check_key(request: Request):
    """Require x-api-key when EXTRACTOR_API_KEY is set (Lambda/prod)."""
    if not API_KEY:
        return  # local dev without key
    provided = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
    if not hmac.compare_digest(provided, API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid x-api-key")


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "region": os.getenv("AWS_REGION", "eu-central-1")}


@app.post("/extract")
async def extract(
    request: Request,
    files: list[UploadFile] = File(...),
    explain: bool = Query(False, description="LLM explanation of disagreements"),
):
    _check_key(request)
    if not files:
        raise HTTPException(400, "upload at least one PDF/PNG/JPG")
    payload = []
    for f in files:
        data = await f.read()
        if not data:
            raise HTTPException(400, f"empty file: {f.filename}")
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(400, f"file too large (15MB max): {f.filename}")
        payload.append((f.filename or "upload.bin", data))
    try:
        result = run_uploads(payload, explain=explain)
    except Exception as e:
        raise HTTPException(500, f"extraction failed: {e}") from e
    return JSONResponse(result)


DEMO_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>KB Document Control — Demo</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:880px;margin:2rem auto;padding:0 1rem;color:#111}
 h1{font-size:1.4rem;margin-bottom:.2rem}
 .sub{color:#555;margin-bottom:1.5rem}
 label{display:block;font-weight:600;margin:.8rem 0 .3rem}
 input[type=password],input[type=file]{width:100%;padding:.5rem}
 button{margin-top:1rem;padding:.6rem 1.2rem;background:#0b5fff;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:1rem}
 button:disabled{opacity:.5}
 pre{background:#0f172a;color:#e2e8f0;padding:1rem;border-radius:8px;overflow:auto;max-height:60vh;font-size:.85rem}
 .err{color:#b91c1c;white-space:pre-wrap}
 .ok{color:#047857}
 .row{display:flex;gap:1rem;flex-wrap:wrap}
 .card{border:1px solid #e5e7eb;border-radius:8px;padding:1rem;margin:1rem 0}
</style>
</head>
<body>
<h1>KB Document Control Extractor</h1>
<p class="sub">Demo FinTechers / Komerční banka — dual Textract + Bedrock, reglas deterministas.<br/>
Demo: upload a loan package (PDF/PNG/JPG), paste the API token, run extract.</p>
<div class="card">
<label for="key">API token / Token (x-api-key)</label>
<input id="key" type="password" placeholder="pega el token / paste token" autocomplete="off"/>
<label for="files">Documents / Documentos</label>
<input id="files" type="file" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/*" multiple/>
<button id="go" onclick="run()">Extract / Extraer</button>
<p id="status"></p>
</div>
<pre id="out">{}</pre>
<script>
async function run(){
  const btn=document.getElementById('go');
  const st=document.getElementById('status');
  const out=document.getElementById('out');
  const key=document.getElementById('key').value.trim();
  const files=document.getElementById('files').files;
  if(!key){st.className='err';st.textContent='Token required / Token obligatorio';return;}
  if(!files.length){st.className='err';st.textContent='Select at least one file / Elige al menos un archivo';return;}
  btn.disabled=true; st.className=''; st.textContent='Running… (puede tardar 30–90s)'; out.textContent='';
  const fd=new FormData();
  for(const f of files) fd.append('files', f);
  try{
    const r=await fetch('extract',{method:'POST',headers:{'x-api-key':key},body:fd});
    const text=await r.text();
    let data; try{data=JSON.parse(text);}catch{data={raw:text};}
    out.textContent=JSON.stringify(data,null,2);
    if(r.ok){st.className='ok';st.textContent='OK '+r.status+' — next_step='+(data.recommended_next_step||'?');}
    else{st.className='err';st.textContent='Error '+r.status;}
  }catch(e){st.className='err';st.textContent=String(e);}
  finally{btn.disabled=false;}
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def demo_page():
    return HTMLResponse(DEMO_HTML)


# Lambda entrypoint
try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:  # local without mangum
    handler = None
