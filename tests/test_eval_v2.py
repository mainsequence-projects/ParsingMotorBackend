"""The V2 eval harness runs end to end with oracle replies and never reaches AWS."""

import json

from extractor import api, aws, document_service
from scripts import eval_v2


def test_mock_eval_scores_oracle_replies_perfectly_and_estimates_cost(monkeypatch, tmp_path, audit_dir):
    for module, name in [(aws, "_client"), (aws, "textract_queries"),
                         (document_service, "converse_json"), (api, "API_KEY")]:
        monkeypatch.setattr(module, name, getattr(module, name))   # restored after main() patches
    out = tmp_path / "eval.json"
    result = eval_v2.main(["--mock", "--only", "05", "10", "-o", str(out)])
    s = result["summary"]
    assert s["packages"] == 2 and s["grouping"] == "2/2"
    assert s["pages"] == "16/16" and s["field_accuracy"] == 1.0   # 10 has a separate ID photo file
    assert result["estimated_real_cost"]["total_usd"] > 0
    assert json.loads(out.read_text())["mode"] == "mock"


def test_scoring_is_strict_on_text_but_normalizes_amounts_and_dates():
    schema = eval_v2.CONTRACTS["payslip"]["draft_schema"]
    assert eval_v2.correct(schema, "value", "/net_pay/amount", 56000, "56 000,00 Kč")
    assert eval_v2.correct(schema, "value", "/pay_period/end_date", "2026-09-30", "30.09.2026")
    assert not eval_v2.correct(schema, "full_name", "/employee_name", "Eva Horáková", "Eva Horakova")
