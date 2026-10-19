"""Load, validate and hash rules.yaml. Fails loudly on a bad config."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")
CHECK_SEVERITIES = {"passed", "discrepancy", "warning", "manual_review"}
FIELD_SEVERITIES = CHECK_SEVERITIES | {"missing"}
NEXT_STEP_CONDITIONS = {
    "any_check_discrepancy", "missing_documents_or_pages", "many_field_manual_review",
    "any_warning_or_manual_review", "any_field_missing", "always",
}

# Outcomes the engine emits per rule; config must map every one to a severity.
REQUIRED_OUTCOMES = {
    "FIELD_RECONCILE": {"agree_high_conf", "agree_low_conf", "llm_only", "ocr_only_high_conf",
                        "ocr_only_low_conf", "disagree", "both_missing"},
    "RC_VALID": {"valid", "invalid"},
    "RC_BIRTHDATE_MATCH": {"match", "mismatch"},
    "CONTRACT_INCOME_NOT_LOAN": {"ok", "equal"},
    "ID_NOT_EXPIRED": {"valid", "expired"},
    "XDOC_BIRTH_NUMBER": {"consistent", "inconsistent"},
    "XDOC_FIRST_NAME": {"consistent", "inconsistent"},
    "XDOC_LAST_NAME": {"consistent", "near_miss", "engine_disagreement", "inconsistent"},
    "XDOC_FULL_NAME": {"consistent", "near_miss", "inconsistent"},
    "XDOC_ADDRESS": {"consistent", "inconsistent"},
    "XDOC_INCOME": {"within_tolerance", "out_of_tolerance"},
    "LOAN_AMOUNT_PRESENT": {"present", "absent"},
    "COMPLETENESS": {"complete", "incomplete"},
    "PAGE_CONTINUITY": {"complete", "gap", "no_markers"},
    "UNEXPECTED_DOCUMENTS": {"none", "present"},
}
# Rules added after v0.2 are optional so audit runs that embed older rules still re-evaluate.
OPTIONAL_OUTCOMES = {
    "CONTRACT_SINGLE": {"single", "multiple"},
    "MODEL_PRIMARY": {"primary", "fallback"},
    "XDOC_EMPLOYER": {"consistent", "inconsistent"},
    "DOC_RECENT": {"recent", "stale", "undated"},
    "TAX_CLEAR": {"clear", "has_arrears", "unclear"},
    "XDOC_HOLDER_RC": {"consistent", "near_miss", "inconsistent"},
}
REQUIRED_PARAMS = {
    "FIELD_RECONCILE": {"pass_conf", "amount_ocr_wins_min_conf", "ocr_preferred_min_conf",
                        "ocr_preferred_types", "text_similarity", "amount_tolerance"},
    "RC_VALID": {"modulus", "lengths", "nine_digit_before_year", "century_pivot_yy",
                 "female_month_offsets", "male_month_offsets"},
    "XDOC_LAST_NAME": {"near_miss_ratio"},
    "XDOC_FULL_NAME": {"near_miss_ratio"},
    "XDOC_ADDRESS": {"similarity"},
    "XDOC_INCOME": {"tolerance", "pairs"},
    "COMPLETENESS": {"default_required"},
}


class RulesConfigError(ValueError):
    """Raised when rules.yaml is malformed. Never swallowed."""


@dataclass
class Rules:
    path: str
    version: str               # sha256 of file bytes (first 16 hex)
    sha256: str
    label: str
    raw_text: str
    rules: dict = field(default_factory=dict)   # id -> rule dict
    next_step: dict = field(default_factory=dict)

    def rule(self, rule_id: str) -> dict:
        try:
            return self.rules[rule_id]
        except KeyError as e:
            raise RulesConfigError(f"rule {rule_id!r} not defined in {self.path}") from e

    def params(self, rule_id: str) -> dict:
        return self.rule(rule_id).get("params") or {}

    def severity(self, rule_id: str, outcome: str) -> str:
        sev = self.rule(rule_id)["severity"]
        if outcome not in sev:
            raise RulesConfigError(f"{rule_id}: no severity mapped for outcome {outcome!r}")
        return sev[outcome]

    def check(self, rule_id: str, outcome: str, *, check: str | None = None, detail: str = "",
              page=None, doc_type: str | None = None, evidence: list | None = None) -> dict:
        """Build a check result that always cites rule_id + rules_version."""
        r = self.rule(rule_id)
        name = check or r.get("check") or rule_id.lower()
        return {
            "check_id": f"{rule_id}:{name}:{doc_type or 'package'}",
            "check": name,
            "rule_id": rule_id,
            "rules_version": self.version,
            "outcome": outcome,
            "status": self.severity(rule_id, outcome),
            "detail": detail,
            "page": page,
            "doc_type": doc_type,
            "evidence": evidence or [],
        }


def _err(path, msg):
    raise RulesConfigError(f"{path}: {msg}")


def validate(data, path="rules.yaml") -> None:
    if not isinstance(data, dict):
        _err(path, "top level must be a mapping")
    if data.get("schema_version") != 1:
        _err(path, "schema_version must be 1")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        _err(path, "'rules' must be a non-empty list")
    seen = set()
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            _err(path, f"{where} must be a mapping")
        rid = r.get("id")
        if not isinstance(rid, str) or not rid:
            _err(path, f"{where}.id missing")
        if rid in seen:
            _err(path, f"duplicate rule id {rid}")
        seen.add(rid)
        desc = r.get("description")
        if not isinstance(desc, dict) or not desc.get("es") or not desc.get("en"):
            _err(path, f"{rid}.description needs 'es' and 'en'")
        sev = r.get("severity")
        if not isinstance(sev, dict) or not sev:
            _err(path, f"{rid}.severity must be a non-empty mapping")
        allowed = FIELD_SEVERITIES if r.get("scope") == "field" else CHECK_SEVERITIES
        for outcome, s in sev.items():
            if s not in allowed:
                _err(path, f"{rid}.severity.{outcome}={s!r} not in {sorted(allowed)}")
        params = r.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            _err(path, f"{rid}.params must be a mapping")
        missing_out = (REQUIRED_OUTCOMES.get(rid) or OPTIONAL_OUTCOMES.get(rid, set())) - set(sev)
        if rid == "FIELD_RECONCILE" and "agree_exact_min_conf" in params and "agree_exact" not in sev:
            missing_out = missing_out | {"agree_exact"}
        if missing_out:
            _err(path, f"{rid}.severity missing outcomes {sorted(missing_out)}")
        missing_par = REQUIRED_PARAMS.get(rid, set()) - set(params)
        if missing_par:
            _err(path, f"{rid}.params missing {sorted(missing_par)}")
        for k, v in params.items():
            if k.endswith(("_conf", "tolerance", "similarity", "ratio")) and not (
                    isinstance(v, (int, float)) and 0 <= v <= 1):
                _err(path, f"{rid}.params.{k}={v!r} must be a number in [0,1]")
    missing_rules = set(REQUIRED_OUTCOMES) - seen
    if missing_rules:
        _err(path, f"missing required rules {sorted(missing_rules)}")
    ns = data.get("next_step")
    if not isinstance(ns, dict) or not isinstance(ns.get("order"), list) or not ns["order"]:
        _err(path, "next_step.order must be a non-empty list")
    for j, item in enumerate(ns["order"]):
        if item.get("when") not in NEXT_STEP_CONDITIONS or not item.get("step"):
            _err(path, f"next_step.order[{j}] invalid: {item}")
    if ns["order"][-1].get("when") != "always":
        _err(path, "next_step.order must end with when: always")


def load_rules_text(text: str, path: str = "<inline>") -> Rules:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RulesConfigError(f"{path}: invalid YAML: {e}") from e
    validate(data, path)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Rules(
        path=str(path), version=sha[:16], sha256=sha, label=data.get("label", ""),
        raw_text=text, rules={r["id"]: r for r in data["rules"]}, next_step=data["next_step"],
    )


def load_rules(path: str | os.PathLike | None = None) -> Rules:
    p = Path(path or os.getenv("EXTRACTOR_RULES") or DEFAULT_RULES_PATH)
    if not p.exists():
        raise RulesConfigError(f"rules file not found: {p}")
    return load_rules_text(p.read_text(encoding="utf-8"), str(p))
