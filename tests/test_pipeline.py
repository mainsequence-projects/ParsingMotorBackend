"""Pipeline wiring without AWS (classification/extraction stubbed)."""
from extractor import audit, pipeline


def test_extract_and_runs_use_the_same_reference_date(monkeypatch, audit_dir):
    seen = {}
    real = pipeline.evaluate_package

    def spy(raw_docs, rules, **kw):
        seen.update(kw)
        return real(raw_docs, rules, **kw)
    monkeypatch.setattr(pipeline, "classify_pages", lambda pages: [])
    monkeypatch.setattr(pipeline, "evaluate_package", spy)
    res = pipeline.run_package([], inputs=[], package_id="t")
    assert seen["run_date"] == audit.run_date(res["run_id"])
    assert audit.current_state(res["run_id"])["recommended_next_step"] == res["recommended_next_step"]
