"""Model fallback is visible; odd LLM output never crashes (no AWS)."""
import copy

from extractor.classify import _conf
from extractor.evaluate import evaluate_package
from extractor.rules_engine import load_rules


def test_extraction_fallback_model_is_a_warning(raw03):
    raw = copy.deepcopy(raw03)
    base = evaluate_package(raw, load_rules(), run_date="2026-10-19")
    assert next(c for c in base["checks"] if c["rule_id"] == "MODEL_PRIMARY")["status"] == "passed"
    for v in raw[2]["raw"]["bedrock"].values():
        v["model"] = "eu.amazon.nova-lite-v1:0"
    res = evaluate_package(raw, load_rules(), run_date="2026-10-19")
    c = next(c for c in res["checks"] if c["rule_id"] == "MODEL_PRIMARY")
    assert c["outcome"] == "fallback" and c["status"] == "warning"
    assert c["evidence"][0]["model"] == "eu.amazon.nova-lite-v1:0"
    assert res["recommended_next_step"] != "approve_ready"


def test_classification_confidence_is_robust():
    assert _conf("high") == 0.0 and _conf(None) == 0.0
    assert _conf(95) == 0.95 and _conf(0.7) == 0.7 and _conf(-3) == 0.0 and _conf(1e9) == 1.0
