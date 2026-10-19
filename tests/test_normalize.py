"""Deterministic normalisers: Czech formats and OCR garbage (no AWS)."""
import pytest

from extractor.normalize import norm_amount


@pytest.mark.parametrize("raw,exp", [
    ("52 000,00 Kč", 52000.0), ("52.000,00", 52000.0), ("CZK 52000", 52000.0),
    ("52\u00a0000,00 CZK", 52000.0), ("52 000,- Kč", 52000.0), ("52000,-", 52000.0),
    ("52,000.50", 52000.5), ("250000.00 CZK", 250000.0), ("12 345 678", 12345678.0),
    ("52 000 Kč / měsíc", 52000.0), ("−1 245,00 CZK", -1245.0), ("-1 245,00", -1245.0),
    (52000, 52000.0), (52000.004, 52000.0), ("1,5", 1.5),
])
def test_amount_formats(raw, exp):
    assert norm_amount(raw) == exp


@pytest.mark.parametrize("raw", ["1O000", "15200E.00", "2024: 52000", "52 00 0", "abc", "", None, True])
def test_amount_garbage_is_none_not_a_guess(raw):
    assert norm_amount(raw) is None


@pytest.mark.parametrize("raw,exp", [
    ("1. 5. 1988", "1988-05-01"), ("01.05.1988", "1988-05-01"), ("1.5.1988", "1988-05-01"),
    ("1988-05-01", "1988-05-01"), ("1. května 1988", "1988-05-01"), ("12. dubna 1988", "1988-04-12"),
    ("12 April 1988", "1988-04-12"), ("April 12, 1988", "1988-04-12"), ("28. září 2026", "2026-09-28"),
    ("31.02.1988", None), ("01.05.88", None), ("", None), (None, None),
])
def test_dates_czech_formats(raw, exp):
    from extractor.normalize import norm_date
    assert norm_date(raw) == exp
