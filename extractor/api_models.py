"""Pydantic models of the HTTP contract (what /openapi.json publishes for Jose).

Legacy responses allow additional keys so engine additions do not break clients.
Strict upload request contracts are re-exported from document_models.
"""
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# Re-export the strict upload contracts for existing Python clients.
from .document_models import ExtractJsonRequest, FileB64  # noqa: F401

FieldStatus = Literal["passed", "warning", "manual_review", "missing"]
CheckStatus = Literal["passed", "discrepancy", "warning", "manual_review"]


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ErrorResponse(BaseModel):
    detail: Union[str, List[Any]] = Field(..., description="Human-readable reason (422 from FastAPI validation is a list)")


class Limits(_Open):
    max_files: int
    max_pages: int
    max_file_mb: float


class HealthResponse(_Open):
    status: Literal["ok"]
    version: str = Field(..., examples=["0.3.0"])
    region: str = Field(..., examples=["eu-central-1"])
    git_commit: str
    rules_version: Optional[str] = Field(None, description="sha256[:16] of rules.yaml; null if it fails to load")
    rules_error: Optional[str] = None
    audit_storage: Literal["persistent", "ephemeral"] = Field(
        ..., description="ephemeral = Lambda /tmp: runs may not survive a cold start")
    limits: Limits


class SessionResponse(_Open):
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str
    expiration: str
    region: str
    functionUrl: str
    service: Literal["lambda"]


class PageRef(_Open):
    source_file: str
    page: int = Field(..., description="1-based physical page in source_file (the citation)")
    page_number: Optional[int] = Field(None, description="printed 'Page x of y' marker, if any")
    page_count: Optional[int] = None
    doc_key: Optional[str] = None
    rotation: Optional[int] = None
    confidence: Optional[float] = Field(None, description="classification confidence 0-1")


class FieldResult(_Open):
    type: str
    value: Any = Field(None, description="final value as printed (from the engine in `source`)")
    normalized: Any = None
    textract: Any = None
    textract_conf: float = 0.0
    textract_page: Optional[int] = None
    bedrock: Any = None
    bedrock_page: Optional[int] = None
    source: Optional[Literal["both", "textract", "bedrock"]] = None
    confidence: Optional[float] = Field(None, description="Textract conf of the kept value; null = LLM-only")
    page: Optional[int] = Field(None, description="page cited for `value`")
    outcome: str = Field(..., examples=["agree_high_conf", "agree_exact", "disagree", "llm_only"])
    status: FieldStatus
    rule_ids: List[str]
    rules_version: str
    bbox: Optional[Dict[str, float]] = None
    explanation: Optional[str] = None
    engine: Optional[Dict[str, Any]] = Field(None, description="engine result before a human decision")
    human: Optional[Dict[str, Any]] = None


class Evidence(_Open):
    doc_id: Optional[str] = None
    doc_type: Optional[str] = None
    field: Optional[str] = None
    value: Any = None
    page: Optional[int] = None


class Check(_Open):
    check_id: str = Field(..., examples=["XDOC_INCOME:income_contract_vs_payslip:package"])
    check: str
    rule_id: str
    rules_version: str
    outcome: str
    status: CheckStatus
    detail: str = ""
    page: Optional[int] = None
    doc_type: Optional[str] = None
    evidence: List[Evidence] = []
    engine: Optional[Dict[str, Any]] = None
    human: Optional[Dict[str, Any]] = None
    description: Optional[str] = Field(None, description="Plain-English meaning of the check, from rules.yaml "
                                       "(params entry `summary` > rule `summary` > description.en)",
                                       examples=["Income declared on the contract matches the net income on the payslip."])


class Document(_Open):
    doc_id: str = Field(..., examples=["d1"])
    doc_type: str = Field(..., examples=["contract"])
    source_file: str
    doc_key: Optional[str] = None
    page_start: int
    page_end: int
    pages: List[PageRef]
    classification_confidence: float
    classification_reason: Optional[str] = None
    fields: Dict[str, FieldResult]
    checks: List[Check] = []


class MissingPages(_Open):
    doc_id: str
    doc_type: str
    page_count: int
    missing_page_numbers: List[int]


class Evaluation(_Open):
    rules_version: str
    rules_label: str
    documents: List[Document]
    checks: List[Check]
    required_documents: List[str]
    required_documents_source: Literal["contract", "default"]
    missing_documents: List[str]
    missing_pages: List[MissingPages]
    unexpected_documents: List[str]
    combined_confidence: float
    fields_auto_validated_pct: float = Field(..., description="COMPAT: % of fields with status passed OR warning "
                                             "(not routed to manual review). Not 'passed'; use fields_passed_pct.")
    fields_passed: Optional[int] = Field(None, description="fields with status == passed (e.g. 39)")
    fields_total: Optional[int] = Field(None, description="all fields read (e.g. 48)")
    fields_passed_pct: Optional[float] = Field(None, description="100 * fields_passed / fields_total")
    field_status_counts: Optional[Dict[str, int]] = Field(None, examples=[{"passed": 39, "warning": 9}])
    check_status_counts: Optional[Dict[str, int]] = Field(None, examples=[{"discrepancy": 2, "passed": 30}])
    client_name: Optional[str] = Field(None, description="first document carrying first/last name")
    recommended_next_step: str = Field(..., examples=[
        "approve_ready", "spot_check_warnings", "manual_review_extraction",
        "request_missing_documents", "manual_review_discrepancy"])
    draft_client_message: Optional[str] = Field(None, description="English email draft (compat); null if nothing to ask")
    draft_client_messages: Optional[Dict[str, str]] = Field(None, description='{"cs": "...", "en": "..."}' )


class InputRef(_Open):
    name: str
    original_name: Optional[str] = None
    bytes: int
    sha256: str
    pages: Optional[int] = Field(None, description="pages of this file in the package")


class Timings(_Open):
    ingest: Optional[int] = Field(None, description="ms; null for runs before 0.5.0 (happens before run_started)")
    classify: Optional[int] = None
    extract: Optional[int] = None
    evaluate: Optional[int] = None
    total: Optional[int] = None
    source: Optional[Literal["measured", "event_timestamps"]] = Field(
        None, description="RunState only: measured (0.5.0+) or derived from audit event timestamps")


class ExtractResponse(Evaluation):
    run_id: str = Field(..., examples=["20261019T122100Z-9ad003f8"])
    package_id: str
    engine_version: str
    git_commit: str
    region: str
    models: Dict[str, Any]
    generated_at: str
    inputs: List[InputRef]
    pages_classified: List[Dict[str, Any]]
    cost: Dict[str, Any]
    audit_file: Optional[str] = Field(None, description="audit log file name (server side)")
    timings_ms: Optional[Timings] = Field(None, description="per-stage wall time in ms (0.5.0+)")


class RunInput(InputRef):
    stored: Optional[bool] = Field(None, description="bytes kept for GET /runs/{id}/pages/{n}.png")


class RunPage(_Open):
    n: int = Field(..., description="1-based package page index for GET /runs/{id}/pages/{n}.png")
    source_file: str
    page: int
    sha256: Optional[str] = None


class RunState(Evaluation):
    run_id: str
    decisions: List[Dict[str, Any]] = Field(..., description="human decision events, oldest first")
    # run metadata from run_started / run_completed (additive, 0.5.0+)
    package_id: Optional[str] = None
    engine_version: Optional[str] = None
    git_commit: Optional[str] = None
    region: Optional[str] = None
    models: Optional[Dict[str, Any]] = None
    rules: Optional[Dict[str, Any]] = Field(None, description="{version, label, sha256} of the run's rules")
    inputs: Optional[List[RunInput]] = None
    pages: Optional[List[RunPage]] = None
    cost: Optional[Dict[str, Any]] = Field(None, description="{run: {usd, ...}, cache_hits}")
    timings_ms: Optional[Timings] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    run_status: Optional[Literal["completed", "failed", "running"]] = None
    audit_events: Optional[int] = None
    head_hash: Optional[str] = None


class AuditEventView(_Open):
    seq: int
    ts: str
    type: str = Field(..., examples=["run_started", "classification", "extraction", "evaluation",
                                     "run_completed", "decision", "reevaluation", "run_failed"])
    prev_hash: str
    hash: str
    summary: Dict[str, Any] = Field(..., description="small payload view; heavy payloads stay in the log")


class AuditEventsResponse(_Open):
    run_id: str
    verify: Dict[str, Any] = Field(..., description="{ok, events, head_hash, errors}")
    events: List[AuditEventView]


class RunListItem(_Open):
    run_id: str
    package_id: Optional[str] = None
    run_status: Literal["completed", "failed", "running"]
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    client_name: Optional[str] = None
    contract_number: Optional[str] = None
    next_step: Optional[str] = Field(None, description="current: after the latest human decision, else engine's")
    engine_next_step: Optional[str] = None
    decisions: int = 0
    last_reviewer: Optional[str] = None
    check_status_counts: Optional[Dict[str, int]] = None
    field_status_counts: Optional[Dict[str, int]] = None
    fields_passed: Optional[int] = None
    fields_total: Optional[int] = None
    documents: Optional[int] = None
    doc_types: Optional[List[str]] = None
    required_documents: Optional[int] = None
    missing_documents: Optional[List[str]] = None
    pages: Optional[int] = None
    duration_ms: Optional[int] = None
    usd: Optional[float] = None
    rules_version: Optional[str] = None


class RunsResponse(_Open):
    runs: List[RunListItem]
    total: int = Field(..., description="runs in the index (before filter/limit)")
    storage: Literal["persistent", "ephemeral"]


class EvalSummaryResponse(_Open):
    packages: int
    page_classification_accuracy: float
    field_accuracy: float
    fields: str = Field(..., examples=["518/527"])
    check_accuracy: float
    checks: str
    expected_flags_hit: str
    false_alarms: int
    next_step_accuracy: str
    next_step_safe: str
    eval_usd: float
    rows: List[Dict[str, Any]] = Field(..., description="per package: expected vs actual next step, errors")


class AuditEvent(_Open):
    run_id: str
    seq: int
    ts: str
    type: str
    payload: Dict[str, Any]
    prev_hash: str
    hash: str


class DecisionRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"field": "contract.declared_monthly_income", "action": "edit", "new_value": "52000",
         "reviewer": "jose", "comment": "read from the original"},
        {"check": "XDOC_INCOME:income_contract_vs_payslip:package", "action": "reject",
         "reviewer": "ana", "comment": "bonus month"},
        {"action": "request_documents", "new_value": ["payslip"], "reviewer": "ana"}]})
    field: Optional[str] = Field(None, max_length=120, description="'<doc_type|doc_id>.<field>'")
    check: Optional[str] = Field(None, max_length=200, description="check_id, rule_id or check name")
    action: Literal["confirm", "edit", "reject", "request_documents"]
    new_value: Optional[Union[str, float, int, List[str]]] = Field(
        None, description="edit: new value (field) or status (check); request_documents: doc types")
    reviewer: str = Field(..., min_length=1, max_length=100)
    comment: str = Field("", max_length=2000)


class DecisionResponse(_Open):
    run_id: str
    next_step_before: str
    next_step: str
    decision_event: AuditEvent
    reevaluation_event: AuditEvent


class VerifyResponse(_Open):
    ok: bool
    file: str
    run_id: Optional[str] = None
    events: int
    head_hash: Optional[str] = None
    errors: List[str]


class LearningResponse(_Open):
    runs_scanned: int
    min_support: int
    auto_applied: Literal[False]
    note: str
    per_rule: Dict[str, Any]
    suggestions: List[Dict[str, Any]]


class RuleInfo(_Open):
    id: str
    scope: Optional[str] = None
    check: Optional[str] = None
    summary: str = Field(..., description="plain-English text (summary or description.en)")
    description: Dict[str, str] = Field(..., description="{es, en}")
    checks: Optional[Dict[str, str]] = Field(None, description="check name -> summary, for multi-check rules")


class RulesResponse(_Open):
    rules_version: str
    label: str
    path: str
    yaml: str
    rules: Optional[List[RuleInfo]] = None


class RuleProposalsResponse(_Open):
    rules_version: str
    rules_label: str
    min_support: int
    auto_applied: Literal[False]
    proposals: List[Dict[str, Any]] = Field(..., description="One-value rules.yaml patches with impact on audited runs")
    history: List[Dict[str, Any]] = Field(..., description="Last events of the hash-chained rules ledger")


class RuleReviewRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"action": "approve", "reviewer": "lead_ana", "comment": "bonus months explain the gap"}]})
    action: Literal["approve", "reject"]
    reviewer: str = Field(..., max_length=100)
    comment: str = Field("", max_length=2000)


class RuleReviewResponse(_Open):
    proposal_id: str
    action: str
    rules_version_new: Optional[str]
    path: Optional[str]
    yaml: Optional[str]
    activation: Optional[str] = Field(None, description="How to activate the new version (never automatic)")
    event: Dict[str, Any]
    ledger: Dict[str, Any]

