"""FastAPI surface for Jose / Main Sequence / demo. Lambda via Mangum."""
from __future__ import annotations

import base64
import hmac
import logging
import os
from pathlib import Path

import boto3
from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from . import api_models as M
from .cost import BudgetExceeded
from .ingest import InputError
from .pipeline import run_uploads
from .rules_engine import RulesConfigError

log = logging.getLogger("extractor.api")
app = FastAPI(
    title="KB Document Control Extractor",
    version=__version__,
    description=("Komerční banka back-office document controls. Dual Textract + Bedrock extraction, "
                 "deterministic reconciliation and rules (rules.yaml), hash-chained audit trail and "
                 "human decisions. Auth: header `x-api-key` (when EXTRACTOR_API_KEY is set). "
                 "See docs/API.md."),
)
ERR = {c: {"model": M.ErrorResponse} for c in (400, 401, 404, 409, 413, 415, 422, 500, 502, 503)}

# CORS only when asked for (Jose's frontend on localhost / Main Sequence). NOT on Lambda: the
# Function URL already adds CORS headers and doubled headers make browsers reject the response.
_CORS = [o.strip() for o in os.getenv("EXTRACTOR_CORS_ORIGINS", "").split(",") if o.strip()]
if _CORS:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=_CORS, allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["content-type", "x-api-key"], allow_credentials=False, max_age=600)

API_KEY = os.getenv("EXTRACTOR_API_KEY", "")
REGION = os.getenv("AWS_REGION", "eu-central-1")
DEMO_INVOKE_ROLE_ARN = os.getenv(
    "DEMO_INVOKE_ROLE_ARN",
    "",  # set by deploy
)
FUNCTION_URL = os.getenv("FUNCTION_URL", "")  # Lambda Function URL for browser SigV4 POST


def _check_key(request: Request):
    """Header only: a token in the query string ends up in CloudFront/Lambda logs and history."""
    if not API_KEY:
        return
    provided = request.headers.get("x-api-key") or ""
    # compare bytes: compare_digest(str, str) raises TypeError on non-ASCII input (-> 500)
    if not hmac.compare_digest(provided.encode("utf-8", "surrogateescape"), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="missing or invalid x-api-key")


@app.get("/health", response_model=M.HealthResponse, tags=["meta"])
def health():
    from . import audit, ingest
    from .rules_engine import load_rules
    try:
        rv, rerr = load_rules().version, None
    except RulesConfigError as e:
        rv, rerr = None, str(e)[:300]
    ad = str(audit.audit_dir())
    return {"status": "ok", "version": __version__, "region": REGION,
            "git_commit": audit.git_info()["commit"], "rules_version": rv, "rules_error": rerr,
            "audit_storage": "ephemeral" if ad.startswith("/tmp") else "persistent",
            "limits": {"max_files": ingest.MAX_FILES, "max_pages": ingest.MAX_PAGES,
                       "max_file_mb": ingest.MAX_FILE_MB}}


@app.get("/session", response_model=M.SessionResponse, responses=ERR, tags=["demo"])
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
        log.error("assume_role failed: %s", type(e).__name__)   # no ARN/account in the response
        raise HTTPException(502, "could not mint demo session") from e
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
        # The pipeline is blocking (boto3, PyMuPDF): run it in a worker thread so one
        # 60 s extraction does not freeze /health, /runs and other users' requests.
        return await run_in_threadpool(run_uploads, files_payload, explain=explain)
    except InputError as e:          # bad upload -> 4xx, never a 500
        raise HTTPException(e.status, str(e)) from e
    except BudgetExceeded as e:
        raise HTTPException(503, f"budget guard: {e}") from e
    except RulesConfigError as e:
        raise HTTPException(500, f"rules.yaml invalid: {e}") from e
    except Exception as e:
        # AWS error texts carry account ids / ARNs / model ids: log them, return only the class
        log.exception("extraction failed")
        raise HTTPException(502, f"extraction failed ({type(e).__name__}); see server logs") from e


@app.post("/extract", response_model=M.ExtractResponse, responses=ERR, tags=["extraction"])
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
        payload.append((f.filename or "upload.bin", await f.read()))
    return await _run_files(payload, explain)


_EXTRACT_JSON_BODY = {"requestBody": {"required": True, "content": {"application/json": {"schema": {
    "type": "object", "required": ["files"], "properties": {
        "files": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["content_base64"], "properties": {
                "filename": {"type": "string", "example": "package.pdf"},
                "content_base64": {"type": "string", "format": "byte"}}}},
        "explain": {"type": "boolean", "default": False}}}}}}}


@app.post("/extract_json", response_model=M.ExtractResponse, responses=ERR, tags=["extraction"],
          openapi_extra=_EXTRACT_JSON_BODY)
async def extract_json(request: Request):
    """Same as /extract with JSON + base64 files (used by the browser demo through SigV4)."""
    _check_key(request)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    files = body.get("files") if isinstance(body, dict) else None
    if not isinstance(files, list) or not files:
        raise HTTPException(400, "body must be {\"files\": [{\"filename\", \"content_base64\"}, ...]}")
    payload = []
    for i, item in enumerate(files):
        if not isinstance(item, dict) or not isinstance(item.get("content_base64"), str):
            raise HTTPException(400, f"files[{i}] needs filename + content_base64 (string)")
        name = str(item.get("filename") or "upload.bin")
        try:
            data = base64.b64decode(item["content_base64"], validate=True)
        except Exception as e:
            raise HTTPException(400, f"bad base64 for {name}") from e
        payload.append((name, data))
    return await _run_files(payload, bool(body.get("explain", False)))


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


# ---------------- audit trail & human decisions ----------------
from . import audit as _audit


def _audit_http(e: Exception) -> HTTPException:
    code = (404 if isinstance(e, _audit.NotFound) else 409 if isinstance(e, _audit.Conflict)
            else 422 if isinstance(e, _audit.Invalid) else 400)
    return HTTPException(code, str(e))


@app.post("/runs/{run_id}/decisions", response_model=M.DecisionResponse, responses=ERR, tags=["human-in-the-loop"])
def post_decision(run_id: str, decision: M.DecisionRequest, request: Request):
    """Append a human decision (never mutates) and return the recomputed next step."""
    _check_key(request)
    try:
        r = _audit.record_decision(run_id, decision.model_dump())
    except _audit.AuditError as e:
        raise _audit_http(e) from e
    return {"run_id": run_id, "next_step_before": r["next_step_before"], "next_step": r["next_step"],
            "decision_event": r["decision_event"], "reevaluation_event": r["reevaluation_event"]}


@app.get("/runs/{run_id}", response_model=M.RunState, responses=ERR, tags=["human-in-the-loop"])
def get_run(run_id: str, request: Request):
    _check_key(request)
    try:
        return _audit.current_state(run_id)
    except _audit.AuditError as e:
        raise _audit_http(e) from e


@app.get("/runs/{run_id}/verify", response_model=M.VerifyResponse, responses=ERR, tags=["audit"])
def verify_run(run_id: str, request: Request):
    _check_key(request)
    try:
        if not _audit.run_path(run_id).exists():
            raise _audit.NotFound(f"no audit log for run {run_id}")
        v = dict(_audit.verify(run_id))
        v["file"] = Path(str(v.get("file", ""))).name      # no server paths to clients
        return v
    except _audit.AuditError as e:
        raise _audit_http(e) from e


@app.get("/runs/{run_id}/protocol", responses={**ERR, 200: {"content": {"text/html": {}, "application/pdf": {}},
                                                       "description": "Control protocol (HTML or PDF)"}},
         response_class=HTMLResponse, tags=["audit"])
def get_protocol(run_id: str, request: Request, format: str = Query("html", pattern="^(html|pdf)$")):
    """Cited control protocol: header (case, versions, input hashes), summary, every check with
    evidence from both engines, missing items + client draft (CZ/EN), human decision history.
    Rebuilt from the hash-chained audit log; footer states AI output is not a final approval."""
    from fastapi.responses import Response

    from . import protocol
    _check_key(request)
    try:
        if not _audit.run_path(run_id).exists():
            raise _audit.NotFound(f"no audit log for run {run_id}")
        html = protocol.render_html(protocol.build(run_id))
    except _audit.AuditError as e:
        raise _audit_http(e) from e
    if format == "pdf":
        return Response(protocol.render_pdf(html), media_type="application/pdf",
                        headers={"content-disposition": f'inline; filename="protocol_{run_id}.pdf"'})
    return HTMLResponse(html)


@app.get("/learning", response_model=M.LearningResponse, responses=ERR, tags=["audit"])
def learning(request: Request, min_support: int = Query(3, ge=1, le=1000)):
    """Aggregated human overrides per rule_id with suggestions. Never auto-applied."""
    _check_key(request)
    return _audit.learning_report(min_support=min_support)


@app.get("/rules", response_model=M.RulesResponse, responses=ERR, tags=["meta"])
def get_rules(request: Request):
    _check_key(request)
    from .rules_engine import load_rules
    r = load_rules()
    return {"rules_version": r.version, "label": r.label, "path": Path(r.path).name, "yaml": r.raw_text}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def demo_page():
    return HTMLResponse(DEMO_HTML)


try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = None
