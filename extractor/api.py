"""Classification and typed extraction. Business workflows live in Back Office."""
from __future__ import annotations

import hmac
import logging
import os

import boto3
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, document_comparison, document_service
from . import api_models as M
from . import document_models as D
from . import evidence_contracts as E
from .comparison_contracts import CompareDocumentsRequest, CompareDocumentsResponse
from .cost import BudgetExceeded
from .ingest import InputError

log = logging.getLogger("extractor.api")
app = FastAPI(
    title="Document Extraction API",
    version=__version__,
    description=("Document classification and typed extraction with source evidence. "
                 "Back Office owns requirements, cross-document checks, review decisions and protocols. "
                 "Auth: header x-api-key when EXTRACTOR_API_KEY is set. See docs/API.md."),
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
    from . import ingest
    return {"status": "ok", "version": __version__, "region": REGION,
            "git_commit": os.getenv("EXTRACTOR_GIT_COMMIT", "unknown"),
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


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's default includes the input, potentially echoing entire uploaded documents.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
        for error in exc.errors()
    ]})


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


@app.post("/classify_json", response_model=E.ClassificationResponseV2 | D.ClassifyJsonResponse, responses=ERR,
          tags=["documents"], dependencies=[Depends(_check_key)])
async def classify_json(body: E.ClassifyRequestV2 | D.ClassifyJsonRequest):
    """Discover document types per page from the supplied catalog; no attribute extraction."""
    return await _run_document_operation(document_service.discover, body)


@app.post("/extract_typed_json", response_model=E.ExtractionResponseV2 | D.ExtractTypedJsonResponse, responses=ERR,
          tags=["documents"], dependencies=[Depends(_check_key)])
async def extract_typed_json(body: E.ExtractRequestV2 | D.ExtractTypedJsonRequest):
    """Extract one logical document using an explicit type and complete canonical/draft schemas."""
    return await _run_document_operation(document_service.extract_typed, body)


@app.post("/compare_documents_json", response_model=CompareDocumentsResponse, responses=ERR,
          tags=["documents"], dependencies=[Depends(_check_key)])
async def compare_documents_json(body: CompareDocumentsRequest):
    """Compare cited extracted facts; no business rules, tolerances or decisions."""
    return await _run_document_operation(document_comparison.compare, body)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("docs")


try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = None
