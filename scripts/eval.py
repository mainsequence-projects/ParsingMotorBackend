"""Evaluate the engine on every synthetic package against its ground truth.

  python scripts/eval.py [--cache] [--only 05_scanned_noisy ...] [--from-json]

Metrics (never fed back into the engine):
  page classification accuracy, document grouping, field accuracy (normalised exact match;
  addresses/text use the same similarity as the reconciler), check accuracy (expected flags fired,
  no false discrepancy/manual_review), next step, missing docs/pages, unexpected docs, run cost.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extractor.config import SCHEMAS  # noqa: E402
from extractor.normalize import normalize, values_agree  # noqa: E402

SAMPLES = ROOT / "samples"
OUT = SAMPLES / "out"


STRICTNESS = {"approve_ready": 0, "spot_check_warnings": 1, "manual_review_extraction": 2,
              "request_missing_documents": 3, "manual_review_discrepancy": 3}


def _field_ok(ftype, expected, got):
    e = normalize(ftype, expected) if expected is not None else None
    if e is None:
        return got is None
    if got is None:
        return False
    if ftype in ("address", "text"):
        return values_agree(ftype, e, got, 0.92)
    if ftype == "amount":
        return abs(float(e) - float(got)) < 0.005
    return e == got


def score(res, gt):
    s = {"package": gt["package_id"]}
    # page classification (physical order across inputs)
    got_pages = [p["doc_type"] for p in res["pages_classified"]]
    exp_pages = gt["expected_page_types"]
    s["pages_correct"] = sum(1 for a, b in zip(got_pages, exp_pages) if a == b)
    s["pages_total"] = len(exp_pages)
    # grouping
    got_docs = sorted((d["doc_type"], tuple(p["page"] for p in d["pages"]),
                       d["source_file"]) for d in res["documents"])
    exp_docs = sorted((d["doc_type"], tuple(d["pages"]),
                       d.get("source_file", gt["inputs"][0])) for d in gt["expected_documents"])
    s["grouping_ok"] = got_docs == exp_docs
    # fields
    ok = tot = 0
    wrong = []
    for dt, fields in gt["expected_fields"].items():
        doc = next((d for d in res["documents"] if d["doc_type"] == dt), None)
        for fname, exp in fields.items():
            ftype = SCHEMAS[dt]["fields"][fname].get("type", "text")
            got = (doc["fields"].get(fname) or {}).get("normalized") if doc else None
            tot += 1
            if _field_ok(ftype, exp, got):
                ok += 1
            else:
                wrong.append(f"{dt}.{fname}: exp={exp!r} got={got!r}")
    s["fields_correct"], s["fields_total"], s["field_errors"] = ok, tot, wrong
    # checks
    flagged = {c["check"] for c in res["checks"] if c["status"] != "passed"}
    hard = {c["check"] for c in res["checks"] if c["status"] in ("discrepancy", "manual_review")}
    exp_flags = set(gt.get("expected_flags", []))
    if gt.get("expected_missing"):          # truth says docs are missing -> completeness must fire
        exp_flags.add("required_documents_present")
    if gt.get("expected_missing_pages"):
        exp_flags.add("pages_complete")
    s["flags_expected"] = len(exp_flags)
    s["flags_hit"] = len(exp_flags & flagged)
    s["flags_missed"] = sorted(exp_flags - flagged)
    s["false_alarms"] = sorted(hard - exp_flags)
    s["checks_total"] = len(res["checks"])
    # check accuracy: every check is a binary prediction "this needs attention" (status != passed)
    # vs the truth "this check was seeded to fire" (its name is in the expected flags)
    s["checks_correct"] = sum(1 for c in res["checks"] if (c["status"] != "passed") == (c["check"] in exp_flags))
    s["doc_types"] = len(set(gt.get("required_documents") or gt.get("expected_docs", [])))
    # package level
    s["next_step"] = res["recommended_next_step"]
    s["next_step_expected"] = gt["expected_next_step"]
    s["next_step_ok"] = s["next_step"] == s["next_step_expected"]
    # 'safe' = same or more conservative than expected (a human still looks at it)
    s["next_step_safe"] = STRICTNESS.get(s["next_step"], 9) >= STRICTNESS.get(s["next_step_expected"], 0)
    s["missing_ok"] = sorted(res["missing_documents"]) == sorted(gt.get("expected_missing", []))
    gp = [{k: m[k] for k in ("doc_type", "page_count", "missing_page_numbers")} for m in res["missing_pages"]]
    s["missing_pages_ok"] = gp == gt.get("expected_missing_pages", [])
    s["unexpected_ok"] = sorted(res["unexpected_documents"]) == sorted(gt.get("expected_unexpected", []))
    s["usd"] = res["cost"]["run"]["usd"] if isinstance(res.get("cost"), dict) and "run" in res["cost"] else None
    s["cache_hits"] = res["cost"].get("cache_hits") if isinstance(res.get("cost"), dict) else None
    s["run_id"] = res.get("run_id")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--from-json", action="store_true", help="score existing samples/out/*.json, no AWS")
    ap.add_argument("--out", default=str(OUT), help="where to write per-package JSON + summary")
    a = ap.parse_args()
    if a.cache:
        os.environ["EXTRACTOR_CACHE"] = "1"
    from extractor.cost import BudgetExceeded, snapshot
    from extractor.pipeline import run_path

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for pkg in sorted(p for p in SAMPLES.iterdir() if (p / "ground_truth.json").exists()):
        if a.only and pkg.name not in a.only:
            continue
        gt = json.loads((pkg / "ground_truth.json").read_text())
        out = out_dir / f"{pkg.name}.json"
        if a.from_json:
            res = json.loads(out.read_text())
        else:
            try:
                res = run_path([pkg / i for i in gt["inputs"]], package_id=pkg.name)
            except BudgetExceeded as e:
                print(f"STOP budget: {e}", file=sys.stderr)
                break
            out.write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        s = score(res, gt)
        rows.append(s)
        print(f"{s['package']:<26} pages {s['pages_correct']}/{s['pages_total']}  group {'ok' if s['grouping_ok'] else 'NO'}  "
              f"fields {s['fields_correct']}/{s['fields_total']}  checks {s['checks_correct']}/{s['checks_total']}  flags {s['flags_hit']}/{s['flags_expected']}  "
              f"false {len(s['false_alarms'])}  next {s['next_step']} ({'ok' if s['next_step_ok'] else 'exp ' + s['next_step_expected']})  "
              f"${s['usd']}", flush=True)

    tot = lambda k: sum(r[k] for r in rows)  # noqa: E731
    summary = {
        "packages": len(rows),
        "page_classification_accuracy": round(tot("pages_correct") / max(1, tot("pages_total")), 3),
        "grouping_ok": f"{sum(r['grouping_ok'] for r in rows)}/{len(rows)}",
        "field_accuracy": round(tot("fields_correct") / max(1, tot("fields_total")), 3),
        "fields": f"{tot('fields_correct')}/{tot('fields_total')}",
        "expected_flags_hit": f"{tot('flags_hit')}/{tot('flags_expected')}",
        "check_accuracy": round(tot("checks_correct") / max(1, tot("checks_total")), 3),
        "checks": f"{tot('checks_correct')}/{tot('checks_total')}",
        "false_alarms": sum(len(r["false_alarms"]) for r in rows),
        "packages_without_false_alarm": f"{sum(not r['false_alarms'] for r in rows)}/{len(rows)}",
        "next_step_accuracy": f"{sum(r['next_step_ok'] for r in rows)}/{len(rows)}",
        "next_step_safe": f"{sum(r['next_step_safe'] for r in rows)}/{len(rows)}",
        "missing_docs_ok": f"{sum(r['missing_ok'] for r in rows)}/{len(rows)}",
        "missing_pages_ok": f"{sum(r['missing_pages_ok'] for r in rows)}/{len(rows)}",
        "unexpected_ok": f"{sum(r['unexpected_ok'] for r in rows)}/{len(rows)}",
        "eval_usd": round(sum(r["usd"] or 0 for r in rows), 4),
        "ledger": snapshot(),
        "rows": rows,
    }
    (out_dir / "eval_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2, default=str))


if __name__ == "__main__":
    main()
