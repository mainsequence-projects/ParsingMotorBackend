"""Rodné číslo edge cases (no AWS)."""
import pytest

from extractor.normalize import norm_birth_number
from extractor.rules import parse_birth_number
from extractor.rules_engine import load_rules

P = load_rules().params("RC_VALID")
TODAY = "2026-10-19"


def mk(prefix9):
    return next(prefix9 + str(c) for c in range(10) if int(prefix9 + str(c)) % 11 == 0)


def legacy(prefix_y):
    """A 10-digit number whose first 9 digits leave remainder 10 (check digit 0)."""
    for i in range(1000):
        b = f"{prefix_y}01{(i % 28) + 1:02d}{i:03d}"[:9]
        if int(b) % 11 == 10:
            return b + "0"
    raise AssertionError


@pytest.mark.parametrize("raw", ["885412/0000", "8854120000", "885412 0000"])
def test_with_and_without_slash(raw):
    info = parse_birth_number(norm_birth_number(raw), P, today=TODAY)
    assert info["valid"] and info["birth_date"] == "1988-04-12" and info["sex"] == "F"


def test_nine_digits_only_before_1954():
    assert parse_birth_number("530101123", P, today=TODAY)["valid"]
    assert not parse_birth_number("540101123", P, today=TODAY)["valid"]


def test_future_birth_date_is_invalid():
    info = parse_birth_number(mk("400101123"), P, today=TODAY)   # would be 2040-01-01
    assert not info["valid"] and "future" in info["reason"]


@pytest.mark.parametrize("prefix", ["802215123", "857215123"])   # +20 M / +70 F in the 1980s
def test_extra_offsets_only_since_2004(prefix):
    info = parse_birth_number(mk(prefix), P, today=TODAY)
    assert not info["valid"] and "2004" in info["reason"]


def test_extra_offsets_valid_after_2004():
    assert parse_birth_number(mk("052215123"), P, today=TODAY)["valid"]
    assert parse_birth_number(mk("107215123"), P, today=TODAY)["valid"]


def test_remainder_ten_legacy_rule_only_until_1985():
    old = legacy("85")
    assert parse_birth_number(old, P, today=TODAY)["valid"]
    assert not parse_birth_number(old[:-1] + "1", P, today=TODAY)["valid"]
    new = legacy("05")
    assert not parse_birth_number(new, P, today=TODAY)["valid"]


def test_impossible_date():
    assert not parse_birth_number(mk("881332123"), P, today=TODAY)["valid"]
