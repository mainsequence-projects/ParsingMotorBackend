import yaml
import pytest

from extractor.evaluate import evaluate_package
from extractor.rules import parse_birth_number
from extractor.rules_engine import DEFAULT_RULES_PATH, RulesConfigError, load_rules, load_rules_text

BASE = DEFAULT_RULES_PATH.read_text(encoding="utf-8")


def _variant(mutate):
    data = yaml.safe_load(BASE)
    mutate({r["id"]: r for r in data["rules"]}, data)
    return load_rules_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "variant.yaml")


def _check(res, check_id):
    return next(c for c in res["checks"] if c["check_id"] == check_id)


def test_default_rules_load_and_version_is_hash():
    r = load_rules()
    assert len(r.version) == 16 and r.sha256.startswith(r.version)
    assert "FIELD_RECONCILE" in r.rules and r.next_step["order"][-1]["when"] == "always"


@pytest.mark.parametrize("mutate,msg", [
    (lambda R, d: R["XDOC_INCOME"]["params"].pop("tolerance"), "tolerance"),
    (lambda R, d: R["FIELD_RECONCILE"]["params"].__setitem__("pass_conf", 1.7), "pass_conf"),
    (lambda R, d: R["RC_VALID"]["severity"].__setitem__("invalid", "maybe"), "severity"),
    (lambda R, d: R["RC_VALID"]["description"].pop("es"), "description"),
    (lambda R, d: d["rules"].remove(R["COMPLETENESS"]), "COMPLETENESS"),
    (lambda R, d: d["next_step"]["order"].pop(), "always"),
])
def test_bad_config_fails_loudly(mutate, msg):
    with pytest.raises(RulesConfigError) as e:
        _variant(mutate)
    assert msg in str(e.value)


def test_invalid_yaml_fails():
    with pytest.raises(RulesConfigError):
        load_rules_text("rules: [unclosed", "broken.yaml")


def test_every_check_cites_rule_and_version(raw04):
    r = load_rules()
    res = evaluate_package(raw04, r, run_date="2026-10-19")
    assert res["checks"]
    for c in res["checks"]:
        assert c["rule_id"] in r.rules and c["rules_version"] == r.version
    for d in res["documents"]:
        for f in d["fields"].values():
            assert "FIELD_RECONCILE" in f["rule_ids"]


def test_changing_income_tolerance_changes_outcome(raw03):
    cid = "XDOC_INCOME:income_contract_vs_payslip:package"
    base = evaluate_package(raw03, load_rules(), run_date="2026-10-19")
    assert _check(base, cid)["status"] == "discrepancy"           # 52000 vs 38000, tol 1%
    loose = _variant(lambda R, d: R["XDOC_INCOME"]["params"].__setitem__("tolerance", 0.30))
    res = evaluate_package(raw03, loose, run_date="2026-10-19")
    assert loose.version != base["rules_version"]
    assert _check(res, cid)["status"] == "passed"                  # 26.9% < 30%
    assert _check(res, cid)["rules_version"] == loose.version


def test_changing_pass_conf_changes_field_status(raw03):
    base = evaluate_package(raw03, load_rules(), run_date="2026-10-19")
    cands = [(d["doc_id"], k) for d in base["documents"] for k, f in d["fields"].items()
             if f["outcome"] == "agree_low_conf" and 0.8 <= f["textract_conf"] < 0.9]
    assert cands, "fixture should contain agreeing fields with 0.8<=conf<0.9"
    lower = _variant(lambda R, d: R["FIELD_RECONCILE"]["params"].__setitem__("pass_conf", 0.8))
    res = evaluate_package(raw03, lower, run_date="2026-10-19")
    st = {(d["doc_id"], k): f["status"] for d in res["documents"] for k, f in d["fields"].items()}
    bst = {(d["doc_id"], k): f["status"] for d in base["documents"] for k, f in d["fields"].items()}
    for key in cands:
        assert bst[key] == "warning" and st[key] == "passed"


def _make_rc(prefix9: str) -> str:
    for last in range(10):
        if int(prefix9 + str(last)) % 11 == 0:
            return prefix9 + str(last)
    raise AssertionError


@pytest.mark.parametrize("mm,sex", [("04", "M"), ("24", "M"), ("54", "F"), ("74", "F")])
def test_rc_month_offsets(mm, sex):
    rc = _make_rc(f"88{mm}12000")
    info = parse_birth_number(rc, load_rules().params("RC_VALID"))
    assert info["valid"], info
    assert info["birth_date"] == "1988-04-12" and info["sex"] == sex


def test_rc_offsets_come_from_config():
    rc = _make_rc("882412000")  # +20 offset
    p = load_rules().params("RC_VALID")
    p2 = {**p, "male_month_offsets": []}
    assert parse_birth_number(rc, p)["valid"]
    assert not parse_birth_number(rc, p2)["valid"]


def test_rc_invalid_checksum():
    assert not parse_birth_number("8854120001", load_rules().params("RC_VALID"))["valid"]
