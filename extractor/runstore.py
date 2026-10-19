"""Run metadata, light audit-event views, the runs listing and kept inputs for page renders.

Everything here is read from the same audit storage as the hash-chained logs (EXTRACTOR_AUDIT_DIR):
- `<run_id>.jsonl` (the chain, unchanged format) and `index.jsonl` (one line per event kind),
- `<run_id>.inputs/` with the uploaded bytes, kept so a reviewer UI can show the cited page.
On Lambda that directory is /tmp: per container and ephemeral, like the runs themselves.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from . import audit

KEEP_INPUTS = os.getenv("EXTRACTOR_KEEP_INPUTS", "1") == "1"
KEEP_INPUTS_MAX_MB = float(os.getenv("EXTRACTOR_KEEP_INPUTS_MAX_MB", "40"))   # per run
RENDER_DPI_DEFAULT, RENDER_DPI_MAX = 110, 200
STAGES = ("ingest", "classify", "extract", "evaluate", "total")


# ---------------- timings ----------------
def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _ms(a: str, b: str) -> int:
    return max(0, round((_ts(b) - _ts(a)).total_seconds() * 1000))


def derive_timings(events: list[dict]) -> dict | None:
    """Stage durations from event timestamps (runs written before timings_ms existed).

    ingest happens before run_started, so it is unknown (null) for these runs."""
    by = {}
    for e in events:
        by.setdefault(e["type"], []).append(e)
    if not all(k in by for k in ("run_started", "classification", "evaluation", "run_completed")):
        return None
    t0, tc = by["run_started"][0]["ts"], by["classification"][0]["ts"]
    tx = by["extraction"][-1]["ts"] if by.get("extraction") else tc
    te, tend = by["evaluation"][0]["ts"], by["run_completed"][0]["ts"]
    return {"ingest": None, "classify": _ms(t0, tc), "extract": _ms(tc, tx),
            "evaluate": _ms(tx, te), "total": _ms(t0, tend), "source": "event_timestamps"}


# ---------------- run metadata ----------------
def _pages_per_input(start: dict) -> dict:
    n: dict = {}
    for p in start.get("pages") or []:
        n[p["source_file"]] = n.get(p["source_file"], 0) + 1
    return n


def run_meta(events: list[dict]) -> dict:
    """Additive RunState keys from run_started / run_completed (never re-evaluated)."""
    start = events[0]["payload"] if events and events[0]["type"] == "run_started" else {}
    done = next((e for e in events if e["type"] == "run_completed"), None)
    failed = next((e for e in events if e["type"] == "run_failed"), None)
    ppi = _pages_per_input(start)
    rid = events[0]["run_id"] if events else None
    inputs = [{**i, "pages": ppi.get(i.get("name"), 0),
               "stored": bool(rid) and is_stored(rid, i.get("sha256"))}
              for i in start.get("inputs") or []]
    pages = [{"n": k + 1, "source_file": p["source_file"], "page": p["page"], "sha256": p.get("sha256")}
             for k, p in enumerate(start.get("pages") or [])]
    timings = (done or {}).get("payload", {}).get("timings_ms")
    if timings:
        timings = {**timings, "source": "measured"}
    else:
        timings = derive_timings(events)
    dp = (done or {}).get("payload", {})
    rules = start.get("rules") or {}
    return {
        "package_id": start.get("package_id"),
        "engine_version": start.get("engine_version"),
        "git_commit": (start.get("git") or {}).get("commit"),
        "region": start.get("region"),
        "models": start.get("models") or {},
        "rules": {k: rules.get(k) for k in ("version", "label", "sha256")},
        "inputs": inputs,
        "pages": pages,
        "cost": {"run": dp.get("cost"), "cache_hits": dp.get("cache_hits")} if done else None,
        "timings_ms": timings,
        "started_at": events[0]["ts"] if events else None,
        "completed_at": done["ts"] if done else None,
        "run_status": "completed" if done else "failed" if failed else "running",
        "audit_events": len(events),
        "head_hash": events[-1]["hash"] if events else None,
    }


# ---------------- light event list ----------------
def _counts(items) -> dict:
    out: dict = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


def event_summary(e: dict) -> dict:
    """Small, UI-sized payload view. Heavy payloads (rules text, per-page lists, raw engine
    answers) stay in the log; the hashes here still let anyone check the chain."""
    t, p = e["type"], e.get("payload") or {}
    if t == "run_started":
        s = {"package_id": p.get("package_id"), "engine_version": p.get("engine_version"),
             "git_commit": (p.get("git") or {}).get("commit"), "region": p.get("region"),
             "rules": {k: (p.get("rules") or {}).get(k) for k in ("version", "label")},
             "models": {k: v for k, v in (p.get("models") or {}).items() if isinstance(v, str)},
             "inputs": [{"name": i.get("name"), "sha256": i.get("sha256"), "bytes": i.get("bytes")}
                        for i in p.get("inputs") or []],
             "pages": len(p.get("pages") or [])}
    elif t == "classification":
        pg = p.get("pages") or []
        s = {"pages": len(pg), "doc_types": _counts(x.get("doc_type") for x in pg),
             "cache_hits": sum(1 for x in pg if x.get("cache_hit"))}
    elif t == "extraction":
        d, c = p.get("document") or {}, p.get("calls") or {}
        b = c.get("bedrock") or {}
        s = {"doc_id": d.get("doc_id"), "doc_type": d.get("doc_type"), "source_file": d.get("source_file"),
             "page_start": d.get("page_start"), "page_end": d.get("page_end"),
             "textract_pages": len(c.get("textract") or []),
             "textract_cache_hits": sum(1 for x in c.get("textract") or [] if x.get("cache_hit")),
             "bedrock_model": b.get("model"), "bedrock_cache_hit": b.get("cache_hit")}
    elif t == "evaluation":
        s = {"rules_version": p.get("rules_version"), "next_step": p.get("next_step"),
             "fields": len(p.get("fields") or []),
             "field_status_counts": _counts(f.get("status") for f in p.get("fields") or []),
             "check_status_counts": _counts(c.get("status") for c in p.get("checks") or []),
             "missing_documents": p.get("missing_documents") or [],
             "missing_pages": len(p.get("missing_pages") or []),
             "unexpected_documents": p.get("unexpected_documents") or []}
    elif t == "run_completed":
        s = {"usd": (p.get("cost") or {}).get("usd"), "cache_hits": p.get("cache_hits"),
             "timings_ms": p.get("timings_ms")}
    elif t == "run_failed":
        s = {"error": str(p.get("error", ""))[:200]}
    elif t == "decision":
        s = {k: p.get(k) for k in ("kind", "target", "action", "new_value", "reviewer", "comment",
                                   "next_step_before", "rules_version")}
    elif t == "reevaluation":
        s = {k: p.get(k) for k in ("trigger_event_hash", "next_step", "missing_documents",
                                   "check_status_counts")}
    else:
        s = {"keys": sorted(p)}
    return {k: e.get(k) for k in ("seq", "ts", "type", "prev_hash", "hash")} | {"summary": s}


def events_view(run_id: str) -> dict:
    events = audit.read_events(run_id)
    v = audit.verify(run_id)
    return {"run_id": run_id, "verify": {"ok": v["ok"], "events": v["events"], "head_hash": v["head_hash"],
                                         "errors": v["errors"]},
            "events": [event_summary(e) for e in events]}


# ---------------- runs listing ----------------
def completed_summary(result: dict, pages: int, timings: dict) -> dict:
    """What the dashboard queue needs, written into the run_completed index line."""
    return {"client_name": result.get("client_name"),
            "next_step": result["recommended_next_step"],
            "check_status_counts": result.get("check_status_counts"),
            "field_status_counts": result.get("field_status_counts"),
            "fields_passed": result.get("fields_passed"), "fields_total": result.get("fields_total"),
            "documents": len(result["documents"]),
            "doc_types": sorted({d["doc_type"] for d in result["documents"]}),
            "required_documents": len(result.get("required_documents") or []),
            "missing_documents": result.get("missing_documents") or [],
            "pages": pages, "duration_ms": (timings or {}).get("total"),
            "contract_number": _contract_number(result)}


def _contract_number(result) -> str | None:
    for d in result.get("documents") or []:
        if d.get("doc_type") == "contract":
            return ((d.get("fields") or {}).get("contract_number") or {}).get("value")
    return None


def _from_log(run_id: str) -> dict:
    """Fallback summary for runs indexed before 0.5.0 (no summary in the index line)."""
    try:
        events = audit.read_events(run_id)
    except audit.AuditError:
        return {}
    ev = next((e for e in events if e["type"] == "evaluation"), None)
    if not ev:
        return {}
    p = ev["payload"]
    docs = {(f["doc_id"], f["doc_type"]) for f in p.get("fields") or []}
    st = [f.get("status") for f in p.get("fields") or []]
    t = derive_timings(events) or {}
    return {"next_step": p.get("next_step"),
            "check_status_counts": _counts(c.get("status") for c in p.get("checks") or []),
            "field_status_counts": _counts(st),
            "fields_passed": sum(1 for s in st if s == "passed"), "fields_total": len(st),
            "documents": len(docs), "doc_types": sorted({t_ for _, t_ in docs}),
            "missing_documents": p.get("missing_documents") or [],
            "pages": len(events[0]["payload"].get("pages") or []) if events else None,
            "duration_ms": t.get("total"),
            "client_name": _client_from_fields(p.get("fields") or [])}


def _client_from_fields(rows) -> str | None:
    """Same rule as evaluate: first document carrying a first/last name."""
    for doc_id in dict.fromkeys(r["doc_id"] for r in rows):
        names = {r["field"]: r.get("final") for r in rows if r["doc_id"] == doc_id}
        fn, ln = names.get("first_name"), names.get("last_name")
        if fn or ln:
            return " ".join(str(x) for x in (fn, ln) if x)
    return None


def list_runs(limit: int = 50, next_step: str | None = None, base: Path | None = None) -> dict:
    base = base or audit.audit_dir()
    idx = base / "index.jsonl"
    runs: dict = {}
    if idx.exists():
        for line in idx.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = e.get("run_id")
            if not isinstance(rid, str) or not audit.RUN_ID_RE.match(rid):
                continue
            r = runs.setdefault(rid, {"run_id": rid, "package_id": e.get("package_id"), "run_status": "running",
                                      "started_at": None, "decisions": 0})
            ev = e.get("event")
            if ev == "run_started":
                r["started_at"] = e.get("ts")
                r["package_id"] = e.get("package_id") or r["package_id"]
                r["rules_version"] = e.get("rules_version")
            elif ev == "run_completed":
                r.update(e.get("summary") or {})
                r.update({"run_status": "completed", "completed_at": e.get("ts"), "usd": e.get("usd"),
                          "engine_next_step": e.get("next_step")})
            elif ev == "run_failed":
                r["run_status"] = "failed"
            elif ev == "decision":
                r["decisions"] += 1
                r["human_next_step"] = e.get("next_step")    # after the latest human decision
                r["last_reviewer"] = e.get("reviewer")
    items = sorted(runs.values(), key=lambda r: r.get("started_at") or r["run_id"], reverse=True)
    out = []
    for r in items:
        if r["run_status"] == "completed" and "check_status_counts" not in r:
            r.update({k: v for k, v in _from_log(r["run_id"]).items() if r.get(k) is None})
            r["engine_next_step"] = r.get("engine_next_step") or r.pop("next_step", None)
        r.pop("next_step", None)
        r["next_step"] = r.pop("human_next_step", None) or r.get("engine_next_step")
        if next_step and r.get("next_step") != next_step:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return {"runs": out, "total": len(runs), "storage": "ephemeral" if str(base).startswith("/tmp") else "persistent"}


# ---------------- kept inputs + page render ----------------
def inputs_dir(run_id: str, base: Path | None = None) -> Path:
    return audit.run_path(run_id, base).with_suffix(".inputs")


def _stored_name(sha: str) -> str:
    return f"{sha}.bin"          # content-addressed: upload names never reach the filesystem


def save_inputs(run_id: str, files: list[tuple[str, bytes]]) -> int:
    """Keep the uploaded bytes next to the audit log (size-capped). Returns bytes stored.

    Files are named by sha256, so the name in run_started.inputs maps to the bytes and the
    render endpoint can re-verify them before use."""
    from .ingest import sha256
    if not KEEP_INPUTS or not files:
        return 0
    total = sum(len(b) for _, b in files)
    if total > KEEP_INPUTS_MAX_MB * 1024 * 1024:
        return 0                                   # too big to keep: page renders unavailable
    d = inputs_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    for _, data in files:
        dst = d / _stored_name(sha256(data))
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
    return total


def is_stored(run_id: str, sha: str | None) -> bool:
    return bool(sha) and (inputs_dir(run_id) / _stored_name(sha)).exists()


class PageUnavailable(audit.NotFound):
    """Page exists in the run but its input bytes were not kept (or the container changed)."""


def _filetype(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    raise audit.Invalid("stored input is not a PDF/PNG/JPEG")


def render_page(run_id: str, n: int, dpi: int = RENDER_DPI_DEFAULT) -> tuple[bytes, dict]:
    """PNG of package page n (1-based, order of run_started.pages), upright like the image the
    engines read (classification rotation applied), so normalised bboxes overlay directly."""
    import fitz  # PyMuPDF

    from .ingest import rotate_png, sha256
    events = audit.read_events(run_id)
    start = events[0]["payload"] if events and events[0]["type"] == "run_started" else {}
    pages = start.get("pages") or []
    if not 1 <= n <= len(pages):
        raise audit.NotFound(f"run {run_id} has {len(pages)} pages; no page {n}")
    pg = pages[n - 1]
    meta = next((i for i in start.get("inputs") or [] if i.get("name") == pg["source_file"]), None)
    sha = (meta or {}).get("sha256") or pg.get("source_sha256")
    path = inputs_dir(run_id) / _stored_name(sha or "none")
    if not sha or not path.exists():
        raise PageUnavailable(f"input bytes for {pg['source_file']} were not kept for run {run_id} "
                              "(older run, size cap, EXTRACTOR_KEEP_INPUTS=0, or another Lambda container)")
    data = path.read_bytes()
    if sha256(data) != sha:
        raise audit.Conflict(f"stored input {pg['source_file']} does not match its audited sha256")
    dpi = max(36, min(int(dpi), RENDER_DPI_MAX))
    doc = fitz.open(stream=data, filetype=_filetype(data))
    try:
        png = doc[pg["page"] - 1].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()
    cls = next((e for e in events if e["type"] == "classification"), None)
    rot = next((c.get("rotation") or 0 for c in (cls or {}).get("payload", {}).get("pages") or []
                if c.get("source_file") == pg["source_file"] and c.get("page") == pg["page"]), 0)
    if rot:
        png = rotate_png(png, rot)
    return png, {"source_file": pg["source_file"], "page": pg["page"], "input_sha256": sha,
                 "page_sha256": pg.get("sha256"), "dpi": dpi, "rotation": rot}
