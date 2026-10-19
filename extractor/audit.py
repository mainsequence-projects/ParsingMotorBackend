"""Append-only, hash-chained audit trail (one JSONL file per run + an index).

Every event = {run_id, seq, ts, type, payload, prev_hash, hash} where
hash = sha256(canonical_json(event without 'hash')). Editing, deleting or reordering any line
breaks the chain and `verify` reports it. Human decisions are new events; nothing is mutated.
The learning report only SUGGESTS rule changes; it never edits rules.yaml.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64
ACTIONS = ("confirm", "edit", "reject", "request_documents")
REPO = Path(__file__).resolve().parents[1]


class AuditError(RuntimeError):
    pass


class NotFound(AuditError):
    """Unknown run_id (HTTP 404)."""


class Conflict(AuditError):
    """Run exists but cannot take a decision now: broken chain / not completed (HTTP 409)."""


class Invalid(AuditError):
    """Decision is well-formed JSON but semantically invalid (HTTP 422)."""


RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
MAX_REVIEWER, MAX_COMMENT, MAX_VALUE = 100, 2000, 500


def audit_dir() -> Path:
    d = Path(os.getenv("EXTRACTOR_AUDIT_DIR") or (REPO / "audit"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = Path("/tmp/extractor_audit")
        d.mkdir(parents=True, exist_ok=True)
    return d


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def event_hash(ev: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in ev.items() if k != "hash"}).encode()).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def run_date(run_id: str) -> str:
    """UTC date encoded in the run_id: the single reference date for date rules of a run."""
    return f"{run_id[0:4]}-{run_id[4:6]}-{run_id[6:8]}"


def git_info() -> dict:
    env = os.getenv("EXTRACTOR_GIT_COMMIT")
    if env:
        return {"commit": env, "dirty": None}
    try:
        c = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=5).stdout.strip()
        s = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        return {"commit": c or "unknown", "dirty": bool(s)}
    except Exception:
        return {"commit": "unknown", "dirty": None}


def run_path(run_id: str, base: Path | None = None) -> Path:
    # Strict format: blocks traversal and names like 'index' (the index file is not a run).
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise NotFound(f"unknown run_id {str(run_id)[:80]!r}")
    return (base or audit_dir()) / f"{run_id}.jsonl"


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _last_event(f):
    f.seek(0)
    last = None
    for line in f:
        if line.strip():
            last = line
    return json.loads(last) if last else None


def append(run_id: str, etype: str, payload: dict, base: Path | None = None) -> dict:
    return append_to(run_path(run_id, base), run_id, etype, payload)


def append_to(path: Path, run_id: str, etype: str, payload: dict) -> dict:
    """Hash-chained append to any log file (runs, and the rules change ledger)."""
    with _locked(path) as f:
        last = _last_event(f)
        ev = {"run_id": run_id, "seq": (last["seq"] + 1) if last else 0, "ts": utcnow(),
              "type": etype, "payload": payload, "prev_hash": last["hash"] if last else GENESIS}
        ev["hash"] = event_hash(ev)
        f.seek(0, os.SEEK_END)
        f.write(canonical(ev) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return ev


def index_append(entry: dict, base: Path | None = None):
    with _locked((base or audit_dir()) / "index.jsonl") as f:
        f.seek(0, os.SEEK_END)
        f.write(canonical({"ts": utcnow(), **entry}) + "\n")


def read_events(run_id: str | None = None, path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else run_path(run_id)
    if not p.exists():
        raise NotFound(f"no audit log for run {run_id or p}")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def verify(run_id: str | None = None, path: str | Path | None = None) -> dict:
    p = Path(path) if path else run_path(run_id)
    errors, prev, n = [], GENESIS, 0
    rid = None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {"ok": False, "file": str(p), "events": 0, "errors": ["file not found"]}
    for i, line in enumerate(l for l in lines if l.strip()):
        n += 1
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"line {i}: not JSON")
            prev = None
            continue
        rid = rid or ev.get("run_id")
        if ev.get("seq") != i:
            errors.append(f"line {i}: seq {ev.get('seq')} != {i} (deleted/reordered event)")
        if ev.get("run_id") != rid:
            errors.append(f"line {i}: run_id changed")
        if prev is not None and ev.get("prev_hash") != prev:
            errors.append(f"line {i}: prev_hash does not match previous event")
        if event_hash(ev) != ev.get("hash"):
            errors.append(f"line {i}: hash mismatch (content modified)")
        prev = ev.get("hash")
    if n == 0:
        errors.append("empty log")
    return {"ok": not errors, "file": str(p), "run_id": rid, "events": n,
            "head_hash": prev, "errors": errors}


# ---------------- state reconstruction & decisions ----------------
def _rules_for_run(events, rules=None):
    from .rules_engine import load_rules_text
    if rules is not None:
        return rules
    start = events[0]["payload"]
    return load_rules_text(start["rules"]["text"], start["rules"].get("path", "<audit>"))


def _raw_docs(events):
    return [e["payload"]["document"] for e in events if e["type"] == "extraction"]


def _decisions(events):
    fo, co, req = [], [], []
    for e in events:
        if e["type"] != "decision":
            continue
        d = e["payload"]
        item = {"target": d["target"], "action": d["action"], "new_value": d.get("new_value"),
                "reviewer": d.get("reviewer"), "event_hash": e["hash"]}
        if d["action"] == "request_documents":
            v = d.get("new_value") or []
            req += [x.strip() for x in (v.split(",") if isinstance(v, str) else v) if x and x.strip()]
        elif d["kind"] == "field":
            fo.append(item)
        else:
            co.append(item)
    return fo, co, req


def current_state(run_id: str, rules=None, path=None) -> dict:
    from .evaluate import evaluate_package
    events = read_events(run_id, path)
    r = _rules_for_run(events, rules)
    fo, co, req = _decisions(events)
    res = evaluate_package(_raw_docs(events), r, field_overrides=fo, check_overrides=co,
                           requested_documents=req, run_date=run_date(run_id or events[0]["run_id"]))
    res["run_id"] = run_id
    res["decisions"] = [e["payload"] | {"event_hash": e["hash"], "ts": e["ts"]}
                        for e in events if e["type"] == "decision"]
    return res


def _snapshot_target(state, kind, target):
    if kind == "field":
        who, field = target.split(".", 1)
        for d in state["documents"]:
            if (d["doc_id"] == who or d["doc_type"] == who) and field in d["fields"]:
                f = d["fields"][field]
                return {"doc_id": d["doc_id"], "doc_type": d["doc_type"], "field": field,
                        "value": f["value"], "status": f["status"], "outcome": f["outcome"],
                        "textract_conf": f["textract_conf"], "textract": f["textract"],
                        "bedrock": f["bedrock"], "rule_ids": f["rule_ids"]}
        raise Invalid(f"field not found in this run: {target}")
    hits = [c for c in state["checks"] if target in (c["check_id"], c["rule_id"], c["check"])]
    if not hits:
        raise Invalid(f"check not found in this run: {target}")
    return [{"check_id": c["check_id"], "rule_id": c["rule_id"], "outcome": c["outcome"],
             "status": c["status"], "detail": c["detail"]} for c in hits]


def _validate_decision(decision: dict, state: dict) -> tuple[str, str, object]:
    from .config import DOC_TYPES, SCHEMAS
    from .normalize import normalize
    from .rules_engine import CHECK_SEVERITIES
    action = decision.get("action")
    if action not in ACTIONS:
        raise Invalid(f"action must be one of {ACTIONS}")
    reviewer = decision.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise Invalid("reviewer is required")
    if len(reviewer) > MAX_REVIEWER or len(str(decision.get("comment") or "")) > MAX_COMMENT:
        raise Invalid(f"reviewer max {MAX_REVIEWER} chars, comment max {MAX_COMMENT} chars")
    nv = decision.get("new_value")
    if nv is not None and len(str(nv)) > MAX_VALUE:
        raise Invalid(f"new_value max {MAX_VALUE} chars")
    field, check = decision.get("field"), decision.get("check")
    if action == "request_documents":
        items = [x.strip() for x in (nv.split(",") if isinstance(nv, str) else (nv or []))
                 if isinstance(x, str) and x.strip()]
        if not items:
            raise Invalid("request_documents needs new_value: list of doc types")
        bad = [x for x in items if x not in DOC_TYPES or x == "other"]
        if bad:
            raise Invalid(f"unknown doc types {bad}; allowed {[t for t in DOC_TYPES if t != 'other']}")
        return "package", field or check or "package", items
    if bool(field) == bool(check):
        raise Invalid("give exactly one of 'field' (doc_type.field or doc_id.field) or 'check'")
    kind, target = ("field", field) if field else ("check", check)
    snap = _snapshot_target(state, kind, target)        # Invalid if the target does not exist
    if action == "edit":
        if nv is None or (isinstance(nv, str) and not nv.strip()):
            raise Invalid("edit needs a non-empty new_value")
        if kind == "field":
            ftype = SCHEMAS[snap["doc_type"]]["fields"][snap["field"]].get("type", "text")
            if normalize(ftype, nv) is None:
                raise Invalid(f"new_value {str(nv)[:60]!r} is not a valid {ftype}")
        elif nv not in CHECK_SEVERITIES:
            raise Invalid(f"for a check, edit new_value must be one of {sorted(CHECK_SEVERITIES)}")
    return kind, target, nv


@contextmanager
def _run_lock(run_id: str):
    """Serialise verify -> decision -> reevaluation for one run (separate lock file)."""
    lock = run_path(run_id).with_suffix(".lock")
    with open(lock, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record_decision(run_id: str, decision: dict) -> dict:
    if not run_path(run_id).exists():
        raise NotFound(f"no audit log for run {run_id}")
    with _run_lock(run_id):
        return _record_decision_locked(run_id, decision)


def _record_decision_locked(run_id: str, decision: dict) -> dict:
    v = verify(run_id)
    if not v["ok"]:
        raise Conflict(f"audit chain broken, refusing to append: {v['errors'][:3]}")
    if not any(e["type"] == "run_completed" for e in read_events(run_id)):
        raise Conflict("run did not complete (failed or still running); nothing to decide on")
    before = current_state(run_id)
    kind, target, nv = _validate_decision(decision, before)
    action = decision["action"]
    engine = _snapshot_target(before, kind, target) if kind != "package" else None
    ev = append(run_id, "decision", {
        "kind": kind, "target": target, "action": action, "new_value": nv,
        "reviewer": decision["reviewer"], "comment": decision.get("comment", ""),
        "before": engine, "next_step_before": before["recommended_next_step"],
        "rules_version": before["rules_version"],
    })
    after = current_state(run_id)
    rev = append(run_id, "reevaluation", {
        "trigger_event_hash": ev["hash"],
        "next_step": after["recommended_next_step"],
        "missing_documents": after["missing_documents"],
        "check_status_counts": _count(c["status"] for c in after["checks"]),
        "after": _snapshot_target(after, kind, target) if kind != "package" else None,
    })
    index_append({"run_id": run_id, "event": "decision", "action": action, "target": target,
                  "reviewer": decision["reviewer"], "next_step": after["recommended_next_step"]})
    return {"decision_event": ev, "reevaluation_event": rev,
            "next_step_before": before["recommended_next_step"],
            "next_step": after["recommended_next_step"]}


def _count(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return dict(d)


# ---------------- learning report (suggestions only) ----------------
def _most_overridden(by_outcome) -> str:
    return max(by_outcome, key=lambda o: by_outcome[o].get("edit", 0) + by_outcome[o].get("reject", 0))


def learning_report(base: Path | None = None, min_support: int = 3) -> dict:
    base = base or audit_dir()
    per_rule = defaultdict(lambda: {"decisions": 0, "confirm": 0, "edit": 0, "reject": 0,
                                    "by_outcome": defaultdict(lambda: defaultdict(int)),
                                    "confirmed_low_conf": [], "overridden_passed_conf": [],
                                    "rejected_details": []})
    runs = 0
    for p in sorted(base.glob("*.jsonl")):
        if not RUN_ID_RE.match(p.stem):          # index.jsonl, rules_ledger.jsonl are not runs
            continue
        try:
            events = read_events(path=p)
        except Exception:
            continue
        runs += 1
        for e in events:
            if e["type"] != "decision" or e["payload"]["action"] == "request_documents":
                continue
            d = e["payload"]
            act = d["action"]
            if d["kind"] == "field":
                b = d["before"] or {}
                rid = "FIELD_RECONCILE"
                s = per_rule[rid]
                s["decisions"] += 1
                s[act] += 1
                s["by_outcome"][b.get("outcome")][act] += 1
                if act == "confirm" and b.get("status") in ("warning", "manual_review"):
                    s["confirmed_low_conf"].append(b.get("textract_conf"))
                if act in ("edit", "reject") and b.get("status") == "passed":
                    s["overridden_passed_conf"].append(b.get("textract_conf"))
            else:
                for c in d["before"] or []:
                    s = per_rule[c["rule_id"]]
                    s["decisions"] += 1
                    s[act] += 1
                    s["by_outcome"][c["outcome"]][act] += 1
                    if act == "reject":
                        s["rejected_details"].append(c["detail"])

    suggestions = []
    for rid, s in per_rule.items():
        overrides = s["edit"] + s["reject"]
        rate = overrides / s["decisions"] if s["decisions"] else 0
        s["override_rate"] = round(rate, 2)
        if s["decisions"] < min_support:
            continue
        if rid == "FIELD_RECONCILE":
            lc = [c for c in s["confirmed_low_conf"] if c]
            if len(lc) >= min_support and not s["overridden_passed_conf"]:
                suggestions.append({"rule_id": rid, "param": "pass_conf",
                                    "suggestion": f"reviewers confirmed {len(lc)} low-confidence fields "
                                                  f"(min Textract conf {min(lc):.2f}); consider lowering "
                                                  f"pass_conf towards {min(lc):.2f} after a wider sample",
                                    "change": {"path": "params.pass_conf", "new": math.floor(round(min(lc) * 100, 6)) / 100}})
            for outcome, acts in s["by_outcome"].items():
                n = sum(acts.values())
                ov = acts.get("edit", 0) + acts.get("reject", 0)
                if n >= min_support and ov / n >= 0.5:
                    suggestions.append({"rule_id": rid, "param": f"severity.{outcome}",
                                        "suggestion": f"humans corrected {ov}/{n} fields with outcome "
                                                      f"'{outcome}'; consider a stricter severity for it "
                                                      "(e.g. manual_review)",
                                        "change": {"path": f"severity.{outcome}", "new": "manual_review"}})
            oc = [c for c in s["overridden_passed_conf"] if c]
            if len(s["overridden_passed_conf"]) >= min_support and oc:
                suggestions.append({"rule_id": rid, "param": "pass_conf",
                                    "suggestion": f"{len(s['overridden_passed_conf'])} auto-passed fields "
                                                  "were corrected by humans; consider raising pass_conf",
                                    "change": {"path": "params.pass_conf",
                                               "new": min(0.99, (math.ceil(round(max(oc) * 100, 6)) + 1) / 100)}})
        elif rate >= 0.5:
            suggestions.append({"rule_id": rid, "suggestion":
                                f"humans overrode {overrides}/{s['decisions']} findings "
                                f"({rate:.0%}); review params/severity of {rid}",
                                "evidence": s["rejected_details"][:5],
                                "change": {"path": f"severity.{_most_overridden(s['by_outcome'])}", "milder": True}})
        elif s["confirm"] == s["decisions"]:
            suggestions.append({"rule_id": rid, "suggestion":
                                f"all {s['decisions']} findings confirmed by humans; rule looks well calibrated"})
    out = {"runs_scanned": runs, "min_support": min_support, "auto_applied": False,
           "note": "Suggestions only. rules.yaml is never modified automatically; a human must edit "
                   "and version it (rules_version changes).",
           "per_rule": {k: {**v, "by_outcome": {o: dict(a) for o, a in v["by_outcome"].items()}}
                        for k, v in per_rule.items()},
           "suggestions": suggestions}
    return out
