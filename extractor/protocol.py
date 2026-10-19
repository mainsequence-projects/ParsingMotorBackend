"""Cited control protocol for one run (HTML, optional PDF) — what the back-office signs.

Built ONLY from the run's audit log (inputs, versions, raw engine outputs, human decisions),
re-evaluated deterministically: the same run always yields the same protocol content.
UI language: English (admin view); client drafts are shown in Czech and English.
"""
from __future__ import annotations

from collections import Counter
from html import escape

from . import audit

FOOTER = "AI output supports the decision; not an automatic final approval."
STATUS_COLOR = {"passed": "#1e7e34", "warning": "#b8860b", "manual_review": "#c0392b",
                "discrepancy": "#c0392b", "missing": "#7f8c8d"}


def build(run_id: str) -> dict:
    events = audit.read_events(run_id)
    started = next((e for e in events if e["type"] == "run_started"), None)
    if started is None:
        raise audit.Conflict("run has no run_started event")
    state = audit.current_state(run_id)
    rules = audit._rules_for_run(events)
    ver = audit.verify(run_id)
    sp = started["payload"]
    return {
        "run_id": run_id, "package_id": sp.get("package_id"), "started_at": started["ts"],
        "generated_at": audit.utcnow(), "engine_version": sp.get("engine_version"),
        "git_commit": (sp.get("git") or {}).get("commit"), "region": sp.get("region"),
        "rules_version": state["rules_version"], "rules_label": state["rules_label"],
        "models": sp.get("models") or {}, "inputs": sp.get("inputs") or [],
        "audit_ok": ver["ok"], "audit_head": ver.get("head_hash"), "audit_events": ver.get("events"),
        "state": state, "rule_text": {rid: (r.get("description") or {}).get("en", "")
                                      for rid, r in rules.rules.items()},
    }


def _badge(status):
    c = STATUS_COLOR.get(status, "#555")
    return f'<span style="color:{c};font-weight:bold">{escape(str(status))}</span>'


def _fmt(v):
    if v is None or v == "":
        return "—"
    if isinstance(v, list):
        return escape(", ".join(map(str, v)))
    return escape(str(v))


def render_html(ctx: dict) -> str:
    st = ctx["state"]
    docs = {d["doc_id"]: d for d in st["documents"]}
    fstat = Counter(f["status"] for d in st["documents"] for f in d["fields"].values())
    cstat = Counter(c["status"] for c in st["checks"])
    h = []
    a = h.append
    a("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Control protocol "
      f"{escape(ctx['run_id'])}</title><style>"
      "body{font-family:DejaVu Sans,Arial,sans-serif;font-size:10pt;margin:24px;color:#111}"
      "h1{font-size:16pt;margin:0 0 4px}h2{font-size:12pt;margin:18px 0 6px;border-bottom:1px solid #999}"
      "table{border-collapse:collapse;width:100%;margin:4px 0}td,th{border:1px solid #bbb;padding:3px 5px;"
      "vertical-align:top;text-align:left;font-size:8.5pt}th{background:#e8eef6}"
      ".meta td{border:none;padding:1px 6px}.small{font-size:8pt;color:#555}"
      "pre{white-space:pre-wrap;font-family:DejaVu Sans,Arial,sans-serif;font-size:9pt;background:#f6f6f6;"
      "padding:6px;border:1px solid #ddd}.foot{margin-top:18px;padding:8px;border:2px solid #c0392b;"
      "font-weight:bold}</style></head><body>")
    a("<h1>Komerční banka — Loan document control protocol</h1>")
    a(f"<div class='small'>Case <b>{_fmt(ctx['package_id'])}</b> · run <b>{escape(ctx['run_id'])}</b></div>")
    a("<h2>1. Header</h2><table class='meta'>")
    for k, v in [("Run started (UTC)", ctx["started_at"]), ("Protocol generated (UTC)", ctx["generated_at"]),
                 ("Engine version", f"{ctx['engine_version']} (git {str(ctx['git_commit'])[:12]})"),
                 ("Rules", f"{ctx['rules_label']} · version {ctx['rules_version']}"),
                 ("Models", ", ".join(f"{k2}: {v2}" for k2, v2 in ctx["models"].items())),
                 ("Region", ctx["region"]),
                 ("Audit chain", f"{'VERIFIED' if ctx['audit_ok'] else 'BROKEN'} · {ctx['audit_events']} events · "
                                 f"head {str(ctx['audit_head'])[:16]}…")]:
        a(f"<tr><td><b>{escape(k)}</b></td><td>{_fmt(v)}</td></tr>")
    a("</table><table><tr><th>Input file</th><th>Bytes</th><th>SHA-256</th></tr>")
    for i in ctx["inputs"]:
        a(f"<tr><td>{_fmt(i.get('original_name') or i.get('name'))}</td><td>{_fmt(i.get('bytes'))}</td>"
          f"<td class='small'>{_fmt(i.get('sha256'))}</td></tr>")
    a("</table>")

    a("<h2>2. Summary</h2><table class='meta'>")
    a(f"<tr><td><b>Recommended next step</b></td><td>{_badge(st['recommended_next_step'])}</td></tr>")
    a(f"<tr><td><b>Fields</b></td><td>{', '.join(f'{k}: {v}' for k, v in sorted(fstat.items()))}</td></tr>")
    a(f"<tr><td><b>Checks</b></td><td>{', '.join(f'{k}: {v}' for k, v in sorted(cstat.items()))}</td></tr>")
    a(f"<tr><td><b>Required documents</b></td><td>{_fmt(st['required_documents'])} "
      f"(source: {escape(st['required_documents_source'])})</td></tr>")
    a(f"<tr><td><b>Missing documents</b></td><td>{_fmt(st['missing_documents'])}</td></tr>")
    gaps = "; ".join(f"{g['doc_type']} pages {g['missing_page_numbers']} of {g['page_count']}" for g in st["missing_pages"])
    a(f"<tr><td><b>Missing pages</b></td><td>{_fmt(gaps)}</td></tr>")
    a(f"<tr><td><b>Unrequested documents</b></td><td>{_fmt(st['unexpected_documents'])}</td></tr></table>")
    a("<table><tr><th>Doc</th><th>Type</th><th>Source file · pages</th></tr>")
    for d in st["documents"]:
        pages = ", ".join(str(p["page"]) for p in d["pages"])
        a(f"<tr><td>{escape(d['doc_id'])}</td><td>{escape(d['doc_type'])}</td>"
          f"<td>{escape(d['source_file'])} · p. {pages}</td></tr>")
    a("</table>")

    a("<h2>3. Checks (every rule applied, with evidence)</h2><table><tr><th>Rule</th><th>Description</th>"
      "<th>Status</th><th>Result</th><th>Evidence (doc · field · page · Textract [conf] · Bedrock · final)</th></tr>")
    order = {"discrepancy": 0, "manual_review": 1, "warning": 2, "passed": 3}
    for c in sorted(st["checks"], key=lambda c: (order.get(c["status"], 9), c["rule_id"])):
        ev = []
        for e in c.get("evidence") or []:
            d = docs.get(e.get("doc_id")) or {}
            f = (d.get("fields") or {}).get(e.get("field")) or {}
            if f:
                ev.append(f"{escape(d.get('doc_type', ''))}·{escape(str(e.get('field')))}·p.{_fmt(f.get('page'))}: "
                          f"T={_fmt(f.get('textract'))} [{f.get('textract_conf', 0):.2f}] · B={_fmt(f.get('bedrock'))}"
                          f" · <b>{_fmt(f.get('value'))}</b>")
            elif e:
                ev.append(_fmt({k: v for k, v in e.items() if v is not None}))
        human = f"<br><i>human: {escape(str(c['human'].get('action')))} by {escape(str(c['human'].get('reviewer')))}</i>" if c.get("human") else ""
        a(f"<tr><td>{escape(c['rule_id'])}<br><span class='small'>{escape(c['check'])}</span></td>"
          f"<td>{escape(ctx['rule_text'].get(c['rule_id'], ''))}</td><td>{_badge(c['status'])}{human}</td>"
          f"<td class='small'>{escape(c['outcome'])}: {escape(str(c.get('detail') or ''))}</td>"
          f"<td class='small'>{'<br>'.join(ev) or '—'}</td></tr>")
    a("</table>")

    a("<h2>4. Extracted fields (cited)</h2>")
    for d in st["documents"]:
        a(f"<div><b>{escape(d['doc_id'])} · {escape(d['doc_type'])}</b></div><table><tr><th>Field</th>"
          "<th>Final value</th><th>Status</th><th>Page</th><th>Textract [conf]</th><th>Bedrock</th><th>Outcome</th></tr>")
        for k, f in d["fields"].items():
            human = " (human)" if f.get("human") else ""
            a(f"<tr><td>{escape(k)}</td><td><b>{_fmt(f.get('value'))}</b></td><td>{_badge(f['status'])}{human}</td>"
              f"<td>{_fmt(f.get('page'))}</td><td>{_fmt(f.get('textract'))} [{f.get('textract_conf', 0):.2f}]</td>"
              f"<td>{_fmt(f.get('bedrock'))}</td><td class='small'>{escape(f['outcome'])}</td></tr>")
        a("</table>")

    a("<h2>5. Missing items and client request draft</h2>")
    drafts = st.get("draft_client_messages")
    if drafts:
        a("<div><b>Czech (to send)</b></div><pre>" + escape(drafts.get("cs", "")) + "</pre>")
        a("<div><b>English</b></div><pre>" + escape(drafts.get("en", "")) + "</pre>")
    else:
        a("<p>Nothing to request from the client.</p>")

    a("<h2>6. Human decision history</h2>")
    if st.get("decisions"):
        a("<table><tr><th>Time (UTC)</th><th>Reviewer</th><th>Action</th><th>Target</th><th>New value</th>"
          "<th>Comment</th><th>Event hash</th></tr>")
        for dcs in st["decisions"]:
            a(f"<tr><td>{_fmt(dcs.get('ts'))}</td><td>{_fmt(dcs.get('reviewer'))}</td><td>{_fmt(dcs.get('action'))}</td>"
              f"<td>{_fmt(dcs.get('target'))}</td><td>{_fmt(dcs.get('new_value'))}</td><td>{_fmt(dcs.get('comment'))}</td>"
              f"<td class='small'>{_fmt(str(dcs.get('event_hash'))[:16])}</td></tr>")
        a("</table>")
    else:
        a("<p>No human decisions recorded yet.</p>")

    a("<h2>7. Sign-off</h2><table class='meta'><tr><td>Reviewed by: ______________________</td>"
      "<td>Date: ____________</td><td>Signature: ______________________</td></tr></table>")
    a(f"<div class='foot'>{escape(FOOTER)}</div>")
    a("<div class='small'>Generated from the hash-chained audit log; re-generating it gives the same content "
      "(except the generation time). Synthetic data only.</div></body></html>")
    return "".join(h)


def render_pdf(html_text: str) -> bytes:
    """HTML -> PDF with PyMuPDF Story (already a dependency; no new service)."""
    import io

    import pymupdf as fitz
    story = fitz.Story(html=html_text)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    rect = fitz.paper_rect("a4")
    where = rect + (28, 28, -28, -28)
    more = True
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()
