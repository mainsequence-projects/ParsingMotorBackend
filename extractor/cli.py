"""CLI.

  python -m extractor <file|folder>... [-o out.json] [--rules rules.yaml] [--cache]
  python -m extractor recheck <run_id> [--rules other.yaml]      # re-apply rules, no AWS
  python -m extractor decide <run_id> --field payslip.last_name --action confirm --reviewer ana
  python -m extractor verify <run_id | path.jsonl>
  python -m extractor learn [--min-support 3]
  python -m extractor protocol <run_id> -o protocol.html|protocol.pdf
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SUB = {"recheck", "decide", "verify", "learn", "extract", "protocol"}


def _dump(obj, out=None):
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)


def _extract(argv):
    p = argparse.ArgumentParser(prog="extractor", description="KB back-office document extractor")
    p.add_argument("paths", nargs="+", help="PDF/image files or folders (one package)")
    p.add_argument("-o", "--out")
    p.add_argument("--rules", help="rules.yaml to use (default: extractor/rules.yaml or $EXTRACTOR_RULES)")
    p.add_argument("--package-id")
    p.add_argument("--explain", action="store_true")
    p.add_argument("--cache", action="store_true",
                   help="content-addressed AWS response cache (.cache/aws). Synthetic data only.")
    p.add_argument("--no-audit", action="store_true")
    a = p.parse_args(argv)
    if a.cache:
        os.environ["EXTRACTOR_CACHE"] = "1"
    from .pipeline import run_path
    from .rules_engine import load_rules
    rules = load_rules(a.rules)
    r = run_path(a.paths, rules=rules, explain=a.explain, package_id=a.package_id,
                 audit_enabled=not a.no_audit)
    _dump(r, a.out)
    if a.out:
        print(f"wrote {a.out} run={r['run_id']} rules={r['rules_version']} next={r['recommended_next_step']} "
              f"missing={r['missing_documents']} pages_missing={r['missing_pages']} "
              f"cost=${r['cost']['run']['usd']:.4f} cumulative=${r['cost']['ledger']['total_usd']:.4f}",
              file=sys.stderr)
    return 0


def _recheck(argv):
    p = argparse.ArgumentParser(prog="extractor recheck")
    p.add_argument("run_id")
    p.add_argument("--rules")
    p.add_argument("-o", "--out")
    a = p.parse_args(argv)
    from . import audit
    from .rules_engine import load_rules
    orig = audit.current_state(a.run_id)
    new = audit.current_state(a.run_id, rules=load_rules(a.rules) if a.rules else None)
    o = {c["check_id"]: c["status"] for c in orig["checks"]}
    changes = [{"check_id": c["check_id"], "rule_id": c["rule_id"], "before": o.get(c["check_id"]),
                "after": c["status"]} for c in new["checks"] if o.get(c["check_id"]) != c["status"]]
    fo = {(d["doc_id"], k): f["status"] for d in orig["documents"] for k, f in d["fields"].items()}
    fchanges = [{"field": f"{d['doc_id']}.{k}", "before": fo.get((d["doc_id"], k)), "after": f["status"]}
                for d in new["documents"] for k, f in d["fields"].items() if fo.get((d["doc_id"], k)) != f["status"]]
    _dump({"run_id": a.run_id, "rules_version_original": orig["rules_version"],
           "rules_version_new": new["rules_version"],
           "next_step_original": orig["recommended_next_step"], "next_step_new": new["recommended_next_step"],
           "check_changes": changes, "field_changes": fchanges}, a.out)
    return 0


def _decide(argv):
    p = argparse.ArgumentParser(prog="extractor decide")
    p.add_argument("run_id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--field", help="doc_type.field or doc_id.field")
    g.add_argument("--check", help="check_id or rule_id")
    p.add_argument("--action", required=True, choices=["confirm", "edit", "reject", "request_documents"])
    p.add_argument("--new-value")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--comment", default="")
    a = p.parse_args(argv)
    from . import audit
    nv = a.new_value
    if a.action == "request_documents" and nv:
        nv = [x.strip() for x in nv.split(",") if x.strip()]
    r = audit.record_decision(a.run_id, {"field": a.field, "check": a.check, "action": a.action,
                                         "new_value": nv, "reviewer": a.reviewer, "comment": a.comment})
    _dump({"next_step_before": r["next_step_before"], "next_step": r["next_step"],
           "decision_event": r["decision_event"]})
    return 0


def _verify(argv):
    p = argparse.ArgumentParser(prog="extractor verify")
    p.add_argument("target", help="run_id or path to a .jsonl audit file")
    a = p.parse_args(argv)
    from . import audit
    r = audit.verify(path=a.target) if a.target.endswith(".jsonl") else audit.verify(a.target)
    _dump(r)
    return 0 if r["ok"] else 2


def _learn(argv):
    p = argparse.ArgumentParser(prog="extractor learn")
    p.add_argument("--min-support", type=int, default=3)
    p.add_argument("--audit-dir")
    p.add_argument("-o", "--out")
    a = p.parse_args(argv)
    from . import audit
    _dump(audit.learning_report(Path(a.audit_dir) if a.audit_dir else None, a.min_support), a.out)
    return 0


def _protocol(argv):
    p = argparse.ArgumentParser(prog="extractor protocol")
    p.add_argument("run_id")
    p.add_argument("-o", "--out", help="output file; .pdf -> PDF, anything else -> HTML (default stdout)")
    a = p.parse_args(argv)
    from . import protocol
    html = protocol.render_html(protocol.build(a.run_id))
    if a.out and a.out.lower().endswith(".pdf"):
        Path(a.out).write_bytes(protocol.render_pdf(html))
    elif a.out:
        Path(a.out).write_text(html, encoding="utf-8")
    else:
        print(html)
    if a.out:
        print(f"wrote {a.out}", file=sys.stderr)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUB:
        cmd, rest = argv[0], argv[1:]
        return {"recheck": _recheck, "decide": _decide, "verify": _verify, "learn": _learn,
                "extract": _extract, "protocol": _protocol}[cmd](rest)
    return _extract(argv)
