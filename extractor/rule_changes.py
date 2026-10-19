"""Rule-learning loop: human decisions -> concrete rules.yaml change -> human review -> new version.

Nothing is applied automatically. `proposals()` turns learning-report suggestions into one-value
patches with their impact on every audited run (re-evaluated offline, no AWS). `review()` records
the human verdict in a hash-chained ledger; an approval writes a NEW rules file. The engine only
uses it when someone points EXTRACTOR_RULES at it or commits it as extractor/rules.yaml.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

import yaml

from . import audit
from .evaluate import evaluate_package
from .rules_engine import Rules, RulesConfigError, load_rules, load_rules_text

LEDGER = "rules_ledger.jsonl"
LEDGER_ID = "rules-ledger"
ACTIONS = ("approve", "reject")
# Milder never goes below 'warning': a finding humans keep rejecting stays visible, it just stops blocking.
SEVERITY_ORDER = ["warning", "manual_review", "discrepancy"]
ACTIVATION = ("Not active yet. To activate: set EXTRACTOR_RULES=<path> or commit the file as "
              "extractor/rules.yaml and redeploy. rules_version changes; old runs keep their own rules.")


def _yaml_scalar(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise RulesConfigError(f"only numbers and words can be proposed, got {v!r}")
    if isinstance(v, str) and not re.fullmatch(r"[a-z_]+", v):
        raise RulesConfigError(f"unsafe value {v!r}")
    return str(v)


def patch_rules_text(text: str, rule_id: str, path: str, new) -> str:
    """Change ONE value in rules.yaml text, keeping every comment; prove nothing else moved."""
    section, key = path.split(".", 1)
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"^\s*-\s+id:\s*{re.escape(rule_id)}\s*$", l)), None)
    if start is None:
        raise RulesConfigError(f"rule {rule_id} not found")
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^\s*-\s+id:", lines[i]) or re.match(r"^\S", lines[i])), len(lines))
    sec = next((i for i in range(start, end) if re.match(rf"^\s+{re.escape(section)}:\s*$", lines[i])), None)
    if sec is None:
        raise RulesConfigError(f"{rule_id}.{section} not found")
    sec_indent = len(lines[sec]) - len(lines[sec].lstrip())
    hit = None
    for j in range(sec + 1, end):
        l = lines[j]
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        if len(l) - len(l.lstrip()) <= sec_indent:
            break
        m = re.match(rf"^(\s+{re.escape(key)}:[ \t]*)([^#\r\n]*?)([ \t]*(#[^\r\n]*)?)(\r?\n)?$", l)
        if m:
            hit = j
            lines[j] = m.group(1) + _yaml_scalar(new) + m.group(3) + (m.group(5) or "")
            break
    if hit is None:
        raise RulesConfigError(f"{rule_id}.{path} not found")
    out = "".join(lines)
    expected = copy.deepcopy(yaml.safe_load(text))
    rule = next(r for r in expected["rules"] if r["id"] == rule_id)
    rule[section][key] = new
    if yaml.safe_load(out) != expected:
        raise RulesConfigError(f"patch of {rule_id}.{path} changed more than one value")
    load_rules_text(out)                                   # same validator as the engine
    return out


def _ledger(base: Path) -> Path:
    return base / LEDGER


def _ledger_events(base: Path) -> list[dict]:
    p = _ledger(base)
    return audit.read_events(path=p) if p.exists() else []


def _completed_runs(base: Path):
    for p in sorted(base.glob("*.jsonl")):
        if not audit.RUN_ID_RE.match(p.stem):
            continue
        try:
            events = audit.read_events(path=p)
        except Exception:
            continue
        if any(e["type"] == "run_completed" for e in events):
            yield p.stem, events


def _statuses(res: dict) -> dict:
    out = {c["check_id"]: c["status"] for c in res["checks"]}
    out |= {f"{d['doc_id']}.{k}": f["status"] for d in res["documents"] for k, f in d["fields"].items()}
    return out


def impact(candidate: Rules, current: Rules, base: Path) -> dict:
    """Re-evaluate every audited run with current vs candidate rules (machine view, no human overrides)."""
    rank = {o["step"]: i for i, o in enumerate(current.next_step["order"])}
    runs, changed, status_changes, less_cautious = 0, [], 0, 0
    for rid, events in _completed_runs(base):
        raw, (_, _, req), day = audit._raw_docs(events), audit._decisions(events), audit.run_date(rid)
        a = evaluate_package(raw, current, requested_documents=req, run_date=day)
        b = evaluate_package(raw, candidate, requested_documents=req, run_date=day)
        runs += 1
        sa, sb = _statuses(a), _statuses(b)
        status_changes += sum(1 for k in sa if sb.get(k) != sa[k])
        na, nb = a["recommended_next_step"], b["recommended_next_step"]
        if na != nb:
            changed.append({"run_id": rid, "next_step_before": na, "next_step_after": nb})
            less_cautious += rank.get(nb, 0) > rank.get(na, 0)
    return {"runs_evaluated": runs, "status_changes": status_changes, "next_step_changes": changed,
            "less_cautious_runs": less_cautious}


def _new_value(change: dict, current):
    if not change.get("milder"):
        return change.get("new")
    if current not in SEVERITY_ORDER or current == SEVERITY_ORDER[0]:
        return None
    return SEVERITY_ORDER[SEVERITY_ORDER.index(current) - 1]


def proposals(rules: Rules | None = None, base: Path | None = None, min_support: int = 3) -> dict:
    rules = rules or load_rules()
    base = base or audit.audit_dir()
    ledger = _ledger_events(base)
    reviewed = {e["payload"]["proposal"]["proposal_id"] for e in ledger}
    # Evidence already used by an approved change must not push the same value again (no ratchet).
    used = {(e["payload"]["proposal"]["rule_id"], e["payload"]["proposal"]["path"]):
            e["payload"]["proposal"]["support"] for e in ledger if e["type"] == "rule_change_approved"}
    report = audit.learning_report(base, min_support)
    support = {rid: s["decisions"] for rid, s in report["per_rule"].items()}
    out = []
    for s in report["suggestions"]:
        ch = s.get("change")
        if not ch:
            continue
        section, key = ch["path"].split(".", 1)
        old = (rules.rules.get(s["rule_id"], {}).get(section) or {}).get(key)
        new = _new_value(ch, old)
        if old is None or new is None or new == old:
            continue
        p = {"rule_id": s["rule_id"], "path": ch["path"], "old": old, "new": new,
             "rationale": s["suggestion"], "evidence": s.get("evidence", []),
             "support": support.get(s["rule_id"], 0), "base_rules_version": rules.version}
        p["proposal_id"] = hashlib.sha256(audit.canonical(
            [rules.version, p["rule_id"], p["path"], new]).encode()).hexdigest()[:12]
        if p["proposal_id"] in reviewed or p["support"] <= used.get((p["rule_id"], p["path"]), -1):
            continue
        try:
            cand = load_rules_text(patch_rules_text(rules.raw_text, p["rule_id"], p["path"], new))
        except RulesConfigError as e:
            p["invalid"] = str(e)
        else:
            p["candidate_rules_version"] = cand.version
            p["impact"] = impact(cand, rules, base)
        out.append(p)
    return {"rules_version": rules.version, "rules_label": rules.label, "min_support": min_support,
            "auto_applied": False, "proposals": out, "history": ledger[-20:]}


def review(proposal_id: str, action: str, reviewer: str, comment: str = "", *,
           rules: Rules | None = None, base: Path | None = None, min_support: int = 3,
           write_to: str | Path | None = None) -> dict:
    """Record a human verdict on a proposal; approval writes a new rules file (never activates it)."""
    reviewer = (reviewer or "").strip()
    if action not in ACTIONS:
        raise audit.Invalid(f"action must be one of {ACTIONS}")
    if not reviewer or len(reviewer) > audit.MAX_REVIEWER:
        raise audit.Invalid(f"reviewer must be 1-{audit.MAX_REVIEWER} chars")
    if len(comment or "") > audit.MAX_COMMENT:
        raise audit.Invalid(f"comment max {audit.MAX_COMMENT} chars")
    rules = rules or load_rules()
    base = base or audit.audit_dir()
    ledger = _ledger(base)
    if ledger.exists() and not audit.verify(path=ledger)["ok"]:
        raise audit.Conflict("rules ledger chain broken, refusing to append")
    p = next((x for x in proposals(rules, base, min_support)["proposals"]
              if x["proposal_id"] == proposal_id), None)
    if p is None:
        raise audit.Conflict(f"unknown, stale or already reviewed proposal {proposal_id[:40]!r}; "
                             "reload /rules/proposals")
    if p.get("invalid"):
        raise audit.Conflict(f"proposal produces invalid rules: {p['invalid']}")
    payload = {"proposal": p, "reviewer": reviewer, "comment": comment or "",
               "rules_version_base": rules.version}
    out = {"proposal_id": proposal_id, "action": action, "rules_version_new": None, "path": None,
           "yaml": None, "activation": None}
    if action == "approve":
        text = patch_rules_text(rules.raw_text, p["rule_id"], p["path"], p["new"])
        new = load_rules_text(text)
        saved = base / "rules" / f"rules-{new.version}.yaml"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(text, encoding="utf-8")
        if write_to:
            Path(write_to).write_text(text, encoding="utf-8")
        payload |= {"rules_version_new": new.version, "rules_sha256_new": new.sha256,
                    "saved_as": saved.name, "written_to": str(write_to) if write_to else None}
        out |= {"rules_version_new": new.version, "path": str(saved), "yaml": text, "activation": ACTIVATION}
    ev = audit.append_to(ledger, LEDGER_ID, {"approve": "rule_change_approved", "reject": "rule_change_rejected"}[action], payload)
    return out | {"event": ev, "ledger": audit.verify(path=ledger)}
