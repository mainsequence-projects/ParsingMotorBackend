"""FastAPI surface for Jose / Main Sequence / demo. Lambda via Mangum."""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

import boto3
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import __version__, document_service
from . import api_models as M
from . import document_models as D
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


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's default includes the input, potentially echoing entire uploaded documents.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
        for error in exc.errors()
    ]})


@app.post("/extract_json", response_model=M.ExtractResponse, responses=ERR, tags=["extraction"],
          dependencies=[Depends(_check_key)])
async def extract_json(body: D.ExtractJsonRequest):
    """Legacy loan workflow with a strictly validated JSON/base64 request."""
    return await _run_files(body.decoded_files(), body.explain)


async def _run_document_operation(operation, body):
    try:
        return await run_in_threadpool(operation, body)
    except InputError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except BudgetExceeded as exc:
        raise HTTPException(503, "document processing budget exceeded") from exc
    except Exception as exc:
        # Provider/schema validation exceptions may contain document content or AWS identifiers.
        log.error("document processing failed (%s)", type(exc).__name__)
        raise HTTPException(502, "document processing failed; invalid or unavailable upstream result") from exc


@app.post("/classify_json", response_model=D.ClassifyJsonResponse, responses=ERR,
          tags=["documents"], dependencies=[Depends(_check_key)])
async def classify_json(body: D.ClassifyJsonRequest):
    """Discover document types per page from the supplied catalog; no attribute extraction."""
    return await _run_document_operation(document_service.discover, body)


@app.post("/extract_typed_json", response_model=D.ExtractTypedJsonResponse, responses=ERR,
          tags=["documents"], dependencies=[Depends(_check_key)])
async def extract_typed_json(body: D.ExtractTypedJsonRequest):
    """Extract one logical document using an explicit type and complete canonical/draft schemas."""
    return await _run_document_operation(document_service.extract_typed, body)


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


@app.get("/runs", response_model=M.RunsResponse, responses=ERR, tags=["audit"])
def list_runs(request: Request, limit: int = Query(50, ge=1, le=500),
              next_step: str | None = Query(None, max_length=60)):
    """Dashboard queue: newest first, from the audit index (run_started / run_completed summary /
    decisions). `next_step` is the current one (after the latest human decision). Same storage as
    the runs: on Lambda it is per container and ephemeral."""
    _check_key(request)
    from . import runstore
    return runstore.list_runs(limit=limit, next_step=next_step)


@app.get("/runs/{run_id}", response_model=M.RunState, responses=ERR, tags=["human-in-the-loop"])
def get_run(run_id: str, request: Request):
    """Current state (re-evaluated with the run's own rules + human decisions) plus run metadata:
    package_id, versions, models, inputs (sha256, pages, stored), pages, cost, timings_ms."""
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


@app.get("/runs/{run_id}/events", response_model=M.AuditEventsResponse, responses=ERR, tags=["audit"])
def run_events(run_id: str, request: Request):
    """Audit timeline: every event with seq, ts, type, prev_hash, hash and a small summary.
    Heavy payloads (rules text, page lists, raw engine answers) are not returned."""
    _check_key(request)
    from . import runstore
    try:
        return runstore.events_view(run_id)
    except _audit.AuditError as e:
        raise _audit_http(e) from e


@app.get("/runs/{run_id}/pages/{n}.png", responses={**ERR, 200: {"content": {"image/png": {}},
                                                              "description": "Rendered page (PNG)"}},
         response_class=Response, tags=["audit"])
def run_page(run_id: str, n: int, request: Request,
             dpi: int = Query(110, ge=36, le=200)):
    """Page n (1-based, RunState.pages[].n) rendered with PyMuPDF from the input bytes kept next to
    the audit log (sha256 re-verified), upright as the engines saw it so normalised bboxes overlay.
    404 if the bytes were not kept (older run, size cap, other Lambda container)."""
    from . import runstore
    _check_key(request)
    try:
        png, meta = runstore.render_page(run_id, n, dpi)
    except _audit.AuditError as e:
        raise _audit_http(e) from e
    return Response(png, media_type="image/png", headers={
        "cache-control": "private, max-age=3600",
        "x-source-file-sha256": meta["input_sha256"], "x-page": str(meta["page"]),
        "x-rotation": str(meta["rotation"])})


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


@app.get("/eval/summary", response_model=M.EvalSummaryResponse, responses=ERR, tags=["meta"])
def eval_summary(request: Request):
    """Offline evaluation over the 12 synthetic packages (samples/): KPI cards and, per package,
    expected vs actual next step. A bundled file, not computed live."""
    import json
    _check_key(request)
    here = Path(__file__).resolve().parent
    for p in (os.getenv("EXTRACTOR_EVAL_SUMMARY"), here / "eval_summary.json",
              here.parent / "samples" / "out" / "eval_summary.json"):
        if p and Path(p).is_file():
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            led = data.get("ledger")
            if isinstance(led, dict) and led.get("ledger"):
                data["ledger"] = {**led, "ledger": Path(str(led["ledger"])).name}   # no server paths
            return data
    raise HTTPException(404, "no eval summary bundled")


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
    rules = []
    for rid, rule in r.rules.items():
        multi = {item["check"]: r.describe(rid, item["check"])
                 for v in (rule.get("params") or {}).values() if isinstance(v, list)
                 for item in v if isinstance(item, dict) and item.get("check")}
        rules.append({"id": rid, "scope": rule.get("scope"), "check": rule.get("check"),
                      "summary": r.describe(rid, rule.get("check")), "description": rule["description"],
                      "checks": multi or None})
    return {"rules_version": r.version, "label": r.label, "path": Path(r.path).name, "yaml": r.raw_text,
            "rules": rules}


@app.get("/rules/proposals", response_model=M.RuleProposalsResponse, responses=ERR, tags=["audit"])
def rule_proposals(request: Request, min_support: int = Query(3, ge=1, le=1000)):
    """Concrete rules.yaml changes learned from human decisions, with offline impact. Never auto-applied."""
    _check_key(request)
    from . import rule_changes
    return rule_changes.proposals(min_support=min_support)


@app.post("/rules/proposals/{proposal_id}/review", response_model=M.RuleReviewResponse, responses=ERR,
          tags=["audit"])
def review_rule_proposal(proposal_id: str, body: M.RuleReviewRequest, request: Request,
                         min_support: int = Query(3, ge=1, le=1000)):
    """Approve (writes a new rules version, not activated) or reject a proposal; logged in the ledger."""
    _check_key(request)
    from . import rule_changes
    try:
        return rule_changes.review(proposal_id, body.action, body.reviewer, body.comment,
                                   min_support=min_support)
    except _audit.AuditError as e:
        raise _audit_http(e) from e


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def demo_page():
    return HTMLResponse(DEMO_HTML)


try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = None
