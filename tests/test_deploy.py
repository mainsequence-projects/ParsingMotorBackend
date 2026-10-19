"""Redeploy must not break the live demo (pure functions, no AWS)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy_mod", ROOT / "deploy" / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_merge_env_keeps_session_settings():
    cur = {"DEMO_INVOKE_ROLE_ARN": "arn:x", "FUNCTION_URL": "https://u/", "EXTRACTOR_API_KEY": "old",
           "STALE": "1"}
    out = deploy.merge_env(cur, {"EXTRACTOR_API_KEY": "old", "BEDROCK_MODEL": "m"})
    assert out["DEMO_INVOKE_ROLE_ARN"] == "arn:x" and out["FUNCTION_URL"] == "https://u/"
    assert out["BEDROCK_MODEL"] == "m" and "STALE" not in out


def test_token_is_reused_from_lambda_when_file_missing(tmp_path):
    f = tmp_path / ".deploy_token"
    tok, src = deploy.resolve_token(f, {"EXTRACTOR_API_KEY": "live-token"})
    assert (tok, src) == ("live-token", "lambda") and f.read_text() == "live-token"
    assert oct(f.stat().st_mode)[-3:] == "600"
    tok2, src2 = deploy.resolve_token(f, {"EXTRACTOR_API_KEY": "other"})
    assert (tok2, src2) == ("live-token", "file")


def test_new_token_only_when_nothing_exists(tmp_path):
    tok, src = deploy.resolve_token(tmp_path / "t", {})
    assert src == "new" and len(tok) > 30
