"""Engine fields/endpoints behind the review screen, dashboard and protocol mockups (0.5.0).

Why: the mockups (docs/mockups) show timings, a runs queue, the audit timeline, page images with
bboxes, plain-English check text and '39/48 fields passed'. Each of those is now a real field or
endpoint; these tests pin them so the frontend can rely on them. Offline: no AWS calls.
"""
import io
import json
import shutil
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from extractor import audit, pipeline, runstore
from extractor import api_models as M
from extractor.rules_engine import RulesConfigError, load_rules, load_rules_text

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
RUN = "20261019T125642Z-161958ed"          # recorded 0.3.x run: no timings, no kept inputs


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_AUDIT_DIR", str(tmp_path))
    shutil.copy(FIX / "audit_03_amount_mismatch.jsonl", tmp_path / f"{RUN}.jsonl")
    from extractor.api import app
    return TestClient(app)


def _pdf(pages=2) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page(width=595, height=842).insert_text((72, 72), f"Strana {i + 1} z {pages}")
    out = doc.tobytes()
    doc.close()
    return out


def _stub_engines(monkeypatch):
    monkeypatch.setattr(pipeline, "classify_pages", lambda pages: [])


# ---- RunState metadata + derived timings for an old run ----
def test_old_run_state_has_metadata_and_event_timings(client):
    st = client.get(f"/runs/{RUN}").json()
    assert st["package_id"] == "03_amount_mismatch"
    assert st["engine_version"] and st["git_commit"] and st["models"]["extraction"]
    assert st["inputs"][0]["name"] == "package.pdf" and st["inputs"][0]["pages"] == 5
    assert st["inputs"][0]["stored"] is False and len(st["inputs"][0]["sha256"]) == 64
    assert [p["n"] for p in st["pages"]] == [1, 2, 3, 4, 5]
    t = st["timings_ms"]
    assert t["source"] == "event_timestamps" and t["ingest"] is None
    assert t["total"] == 33 and t["classify"] == 8          # 12:56:42.979 -> .987, -> 43.012
    assert st["cost"]["run"]["usd"] == 0.0 and st["run_status"] == "completed"
    assert st["rules"]["version"] == st["rules_version"]
    M.RunState.model_validate(st)


def test_every_check_has_a_plain_english_description(client):
    st = client.get(f"/runs/{RUN}").json()
    assert st["checks"] and all(c["description"] for c in st["checks"])
    inc = next(c for c in st["checks"] if c["check"] == "income_contract_vs_payslip")
    assert "contract" in inc["description"].lower() and "payslip" in inc["description"].lower()


def test_fields_kpi_strict_and_compat(client):
    st = client.get(f"/runs/{RUN}").json()
    statuses = [f["status"] for d in st["documents"] for f in d["fields"].values()]
    assert st["fields_total"] == len(statuses)
    assert st["fields_passed"] == statuses.count("passed")
    assert st["fields_passed_pct"] == round(100 * st["fields_passed"] / st["fields_total"], 1)
    assert sum(st["field_status_counts"].values()) == st["fields_total"]
    # the old key stays and keeps its meaning: passed + warning
    auto = statuses.count("passed") + statuses.count("warning")
    assert st["fields_auto_validated_pct"] == round(100 * auto / len(statuses), 1)
    assert sum(st["check_status_counts"].values()) == len(st["checks"])


# ---- audit events ----
def test_events_list_is_light_and_matches_the_chain(client, tmp_path):
    r = client.get(f"/runs/{RUN}/events")
    assert r.status_code == 200, r.text
    body = r.json()
    raw = [json.loads(l) for l in (tmp_path / f"{RUN}.jsonl").read_text().splitlines()]
    assert [e["hash"] for e in body["events"]] == [e["hash"] for e in raw]
    assert [e["prev_hash"] for e in body["events"]] == [e["prev_hash"] for e in raw]
    assert body["verify"]["ok"] and body["verify"]["head_hash"] == raw[-1]["hash"]
    assert len(r.content) < len((tmp_path / f"{RUN}.jsonl").read_bytes()) / 5
    txt = json.dumps(body)
    assert "next_step:" not in txt and "raw" not in body["events"][2]["summary"]   # no rules text / raw
    first = body["events"][0]["summary"]
    assert first["rules"]["version"] and first["pages"] == 5 and "text" not in first["rules"]
    ev = next(e for e in body["events"] if e["type"] == "evaluation")["summary"]
    assert ev["next_step"] == "manual_review_discrepancy" and ev["check_status_counts"]["discrepancy"] >= 1


def test_events_after_a_decision_show_decision_and_reevaluation(client):
    client.post(f"/runs/{RUN}/decisions", json={"check": "XDOC_INCOME", "action": "confirm", "reviewer": "ana"})
    types = [e["type"] for e in client.get(f"/runs/{RUN}/events").json()["events"]]
    assert types[-2:] == ["decision", "reevaluation"]
    d = client.get(f"/runs/{RUN}/events").json()["events"][-2]["summary"]
    assert d["action"] == "confirm" and d["reviewer"] == "ana"


def test_unknown_run_is_404(client):
    assert client.get("/runs/20261019T000000Z-00000000/events").status_code == 404
    assert client.get("/runs/../../etc/events").status_code == 404


# ---- new runs: measured timings, index summary, kept inputs, page render ----
def test_new_run_measures_timings_and_writes_index_summary(audit_dir, monkeypatch):
    _stub_engines(monkeypatch)
    res = pipeline.run_uploads([("Smlouva o úvěru.pdf", _pdf(2))], package_id="pkg-x")
    t = res["timings_ms"]
    assert set(t) == {"ingest", "classify", "extract", "evaluate", "total"}
    assert all(isinstance(v, int) and v >= 0 for v in t.values())
    assert t["total"] >= t["ingest"]
    done = [e for e in audit.read_events(res["run_id"]) if e["type"] == "run_completed"][0]
    assert done["payload"]["timings_ms"] == t
    idx = [json.loads(l) for l in (audit_dir / "index.jsonl").read_text().splitlines()]
    s = [e for e in idx if e["event"] == "run_completed"][0]["summary"]
    assert s["pages"] == 2 and s["next_step"] == res["recommended_next_step"] and "check_status_counts" in s
    assert res["inputs"][0]["pages"] == 2
    st = audit.current_state(res["run_id"])
    assert st["timings_ms"]["source"] == "measured" and st["inputs"][0]["stored"] is True


def test_page_render_from_kept_input_and_sha_check(audit_dir, monkeypatch):
    _stub_engines(monkeypatch)
    from extractor.api import app
    res = pipeline.run_uploads([("a.pdf", _pdf(2)), ("b.pdf", _pdf(1))], package_id="pkg-y")
    c = TestClient(app)
    r = c.get(f"/runs/{res['run_id']}/pages/3.png?dpi=50")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n" and r.headers["x-page"] == "1"
    from PIL import Image
    w, h = Image.open(io.BytesIO(r.content)).size
    assert abs(w - 595 * 50 / 72) <= 1 and abs(h - 842 * 50 / 72) <= 1
    assert c.get(f"/runs/{res['run_id']}/pages/4.png").status_code == 404
    assert c.get(f"/runs/{res['run_id']}/pages/0.png").status_code == 404
    # tampered stored bytes are refused, not rendered
    stored = next(runstore.inputs_dir(res["run_id"]).glob("*.bin"))
    stored.write_bytes(stored.read_bytes() + b"x")
    bad = [p for p in (1, 3) if c.get(f"/runs/{res['run_id']}/pages/{p}.png").status_code == 409]
    assert bad


def test_page_render_unavailable_for_runs_without_kept_inputs(client):
    r = client.get(f"/runs/{RUN}/pages/1.png")
    assert r.status_code == 404 and "not kept" in r.json()["detail"]


def test_inputs_not_kept_over_the_cap(audit_dir, monkeypatch):
    _stub_engines(monkeypatch)
    monkeypatch.setattr(runstore, "KEEP_INPUTS_MAX_MB", 0.000001)
    res = pipeline.run_uploads([("a.pdf", _pdf(1))])
    assert not runstore.inputs_dir(res["run_id"]).exists()
    assert audit.current_state(res["run_id"])["inputs"][0]["stored"] is False


def test_failed_run_is_listed_as_failed(audit_dir, monkeypatch):
    def boom(pages):
        raise RuntimeError("x")
    monkeypatch.setattr(pipeline, "classify_pages", boom)
    with pytest.raises(RuntimeError):
        pipeline.run_uploads([("a.pdf", _pdf(1))], package_id="bad")
    runs = runstore.list_runs()["runs"]
    assert runs[0]["run_status"] == "failed" and runs[0]["package_id"] == "bad"


# ---- runs listing ----
def test_runs_listing_old_index_falls_back_to_the_log_and_tracks_decisions(client, tmp_path):
    (tmp_path / "index.jsonl").write_text(
        json.dumps({"ts": "2026-10-19T12:56:42.980Z", "run_id": RUN, "event": "run_started",
                    "package_id": "03_amount_mismatch", "rules_version": "x", "git_commit": "y"}) + "\n" +
        json.dumps({"ts": "2026-10-19T12:56:43.013Z", "run_id": RUN, "event": "run_completed",
                    "package_id": "03_amount_mismatch", "next_step": "manual_review_discrepancy",
                    "usd": 0.0}) + "\n" + "not json\n")
    r = client.get("/runs").json()
    assert r["total"] == 1
    row = r["runs"][0]
    assert row["next_step"] == "manual_review_discrepancy" and row["client_name"] == "Jana Nováková"
    assert row["pages"] == 5 and row["documents"] == 5 and row["check_status_counts"]["discrepancy"] >= 1
    assert row["fields_total"] and row["duration_ms"] == 33 and row["usd"] == 0.0
    client.post(f"/runs/{RUN}/decisions", json={"field": "contract.declared_monthly_income",
                                                "action": "edit", "new_value": "38000", "reviewer": "jose"})
    row = client.get("/runs").json()["runs"][0]
    assert row["next_step"] == "approve_ready" and row["engine_next_step"] == "manual_review_discrepancy"
    assert row["decisions"] == 1 and row["last_reviewer"] == "jose"
    assert client.get("/runs?next_step=approve_ready").json()["runs"]
    assert client.get("/runs?next_step=request_missing_documents").json()["runs"] == []


def test_runs_listing_new_runs_newest_first(audit_dir, monkeypatch):
    _stub_engines(monkeypatch)
    from extractor.api import app
    a = pipeline.run_uploads([("a.pdf", _pdf(1))], package_id="first")
    b = pipeline.run_uploads([("b.pdf", _pdf(1))], package_id="second")
    rows = TestClient(app).get("/runs?limit=1").json()
    assert rows["total"] == 2 and len(rows["runs"]) == 1
    ids = [r["run_id"] for r in TestClient(app).get("/runs").json()["runs"]]
    assert set(ids) == {a["run_id"], b["run_id"]}
    assert TestClient(app).get("/runs").json()["runs"][0]["pages"] == 1


# ---- eval summary, rules, version ----
def test_eval_summary_is_served_without_server_paths(client):
    r = client.get("/eval/summary")
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["packages"] == 12 and len(e["rows"]) == 12
    assert all("next_step_expected" in row for row in e["rows"])
    assert "/" not in e["ledger"]["ledger"]


def test_rules_endpoint_lists_plain_english_summaries(client):
    rules = client.get("/rules").json()["rules"]
    assert len(rules) == len(load_rules().rules)
    assert all(r["summary"] for r in rules)
    inc = next(r for r in rules if r["id"] == "XDOC_INCOME")
    assert len(inc["checks"]) == 5 and all(inc["checks"].values())


def test_summary_falls_back_to_description_and_must_be_text():
    r = load_rules()
    text = r.raw_text.replace('    summary: "The birth number (rodné číslo) has a valid format and check digit."\n', "")
    assert load_rules_text(text).describe("RC_VALID") == r.rules["RC_VALID"]["description"]["en"]
    with pytest.raises(RulesConfigError):
        load_rules_text(r.raw_text.replace(
            'summary: "The birth number (rodné číslo) has a valid format and check digit."', "summary: ''"))


def test_sample_package_05_reports_39_of_48():
    """The review mockup's '39/48 fields passed' (package 05) re-derived from the stored output."""
    out = json.loads((ROOT / "samples" / "out" / "05_income_mismatch_employer.json").read_text())
    st = [f["status"] for d in out["documents"] for f in d["fields"].values()]
    assert (st.count("passed"), len(st)) == (39, 48)


def test_extract_endpoint_returns_timings_and_validates(audit_dir, monkeypatch):
    import base64
    _stub_engines(monkeypatch)
    from extractor.api import app
    r = TestClient(app).post("/extract_json", json={"files": [
        {"filename": "a.pdf", "content_base64": base64.b64encode(_pdf(1)).decode()}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["timings_ms"]) >= {"ingest", "classify", "extract", "evaluate", "total"}
    assert body["inputs"][0]["pages"] == 1 and body["fields_total"] == 0
    assert TestClient(app).get(f"/runs/{body['run_id']}/pages/1.png").status_code == 200
