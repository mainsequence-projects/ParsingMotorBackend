"""FastAPI surface for Jose / Main Sequence / demo. Lambda via Mangum."""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path

import boto3
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
REGION = os.getenv("AWS_REGION", "eu-central-1")
DEMO_INVOKE_ROLE_ARN = os.getenv(
    "DEMO_INVOKE_ROLE_ARN",
    "",  # set by deploy
)
FUNCTION_URL = os.getenv("FUNCTION_URL", "")  # Lambda Function URL for browser SigV4 POST


def _check_key(request: Request):
    if not API_KEY:
        return
    provided = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
    if not hmac.compare_digest(provided, API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid x-api-key")


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "region": REGION}


@app.get("/session")
def session(request: Request):
    """Mint short-lived creds so the browser can SigV4-POST to the Function URL.

    Needed because the org blocks AuthType NONE and CloudFront OAC breaks POST body signing.
    Requires x-api-key. Credentials last 15 minutes and can only invoke this function.
    """
    _check_key(request)
    if not DEMO_INVOKE_ROLE_ARN or not FUNCTION_URL:
        raise HTTPException(500, "DEMO_INVOKE_ROLE_ARN / FUNCTION_URL not configured")
    sts = boto3.client("sts", region_name=REGION)
    try:
        resp = sts.assume_role(
            RoleArn=DEMO_INVOKE_ROLE_ARN,
            RoleSessionName="kb-demo",
            DurationSeconds=900,
        )
    except Exception as e:
        raise HTTPException(500, f"assume_role failed: {e}") from e
    c = resp["Credentials"]
    return {
        "accessKeyId": c["AccessKeyId"],
        "secretAccessKey": c["SecretAccessKey"],
        "sessionToken": c["SessionToken"],
        "expiration": c["Expiration"].isoformat(),
        "region": REGION,
        "functionUrl": FUNCTION_URL if FUNCTION_URL.endswith("/") else FUNCTION_URL + "/",
        "service": "lambda",
    }


async def _run_files(files_payload, explain: bool):
    try:
        return run_uploads(files_payload, explain=explain)
    except Exception as e:
        raise HTTPException(500, f"extraction failed: {e}") from e


@app.post("/extract")
async def extract(
    request: Request,
    files: list[UploadFile] = File(...),
    explain: bool = Query(False),
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
    return JSONResponse(await _run_files(payload, explain))


@app.post("/extract_json")
async def extract_json(request: Request):
    """JSON + base64 files (preferred through browser SigV4)."""
    _check_key(request)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    files = body.get("files") or []
    if not files:
        raise HTTPException(400, "files: [{filename, content_base64}, ...] required")
    payload = []
    for item in files:
        name = item.get("filename") or "upload.bin"
        try:
            data = base64.b64decode(item["content_base64"])
        except Exception as e:
            raise HTTPException(400, f"bad base64 for {name}: {e}") from e
        if not data:
            raise HTTPException(400, f"empty file: {name}")
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(400, f"file too large (15MB max): {name}")
        payload.append((name, data))
    return JSONResponse(await _run_files(payload, bool(body.get("explain", False))))


DEMO_HTML = r"""<!DOCTYPE html>
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
 .card{border:1px solid #e5e7eb;border-radius:8px;padding:1rem;margin:1rem 0}
</style>
<script type="module">
import { AwsClient } from 'https://esm.sh/aws4fetch@1.0.20';
window._AwsClient = AwsClient;
</script>
</head>
<body>
<h1>KB Document Control Extractor</h1>
<p class="sub">Demo FinTechers / Komerční banka — dual Textract + Bedrock, reglas deterministas.<br/>
Upload a loan package (PDF/PNG/JPG), paste the API token, run extract.</p>
<div class="card">
<label for="key">API token / Token (x-api-key)</label>
<input id="key" type="password" placeholder="pega el token / paste token" autocomplete="off"/>
<label for="files">Documents / Documentos</label>
<input id="files" type="file" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/*" multiple/>
<button id="go" onclick="run()">Extract / Extraer</button>
<p id="status"></p>
</div>
<pre id="out">{}</pre>
<script type="module">
async function fileToB64(f){
  const buf=await f.arrayBuffer();
  const bytes=new Uint8Array(buf);
  let bin=''; const chunk=0x8000;
  for(let i=0;i<bytes.length;i+=chunk){
    bin+=String.fromCharCode(...bytes.subarray(i,i+chunk));
  }
  return btoa(bin);
}
window.run = async function(){
  const btn=document.getElementById('go');
  const st=document.getElementById('status');
  const out=document.getElementById('out');
  const key=document.getElementById('key').value.trim();
  const files=document.getElementById('files').files;
  if(!key){st.className='err';st.textContent='Token required / Token obligatorio';return;}
  if(!files.length){st.className='err';st.textContent='Select at least one file / Elige al menos un archivo';return;}
  btn.disabled=true; st.className=''; st.textContent='Getting session…'; out.textContent='';
  try{
    // 1) Session via CloudFront (GET works with OAC) — returns short-lived IAM creds
    const sResp=await fetch('session',{headers:{'x-api-key':key}});
    if(!sResp.ok) throw new Error('session '+sResp.status+' '+await sResp.text());
    const s=await sResp.json();
    st.textContent='Uploading + extracting… (30–90s)';
    // 2) Build JSON body
    const encoded=[];
    for(const f of files){
      encoded.push({filename:f.name, content_base64: await fileToB64(f)});
    }
    const body=JSON.stringify({files:encoded});
    // 3) SigV4 POST directly to Lambda Function URL (CloudFront OAC breaks POST signatures)
    const client=new window._AwsClient({
      accessKeyId:s.accessKeyId,
      secretAccessKey:s.secretAccessKey,
      sessionToken:s.sessionToken,
      service:s.service,
      region:s.region,
    });
    const r=await client.fetch(s.functionUrl+'extract_json',{
      method:'POST',
      headers:{'content-type':'application/json','x-api-key':key},
      body,
    });
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


try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = None
