"""Czech birth number (rodné číslo) parsing, parameterised by rules.yaml (RC_VALID.params)."""
from __future__ import annotations

from datetime import date

DEFAULT_RC_PARAMS = {
    "modulus": 11, "lengths": [9, 10], "nine_digit_before_year": 1954, "century_pivot_yy": 54,
    "female_month_offsets": [50, 70], "male_month_offsets": [20],
    "allow_legacy_check_digit_zero": True,
}


def parse_birth_number(rc: str, params: dict | None = None) -> dict:
    """Validate a digits-only rodné číslo. Returns {valid, reason, birth_date?, sex?}."""
    p = {**DEFAULT_RC_PARAMS, **(params or {})}
    if not rc or not str(rc).isdigit() or len(rc) not in p["lengths"]:
        return {"valid": False, "reason": f"birth number must have {p['lengths']} digits"}
    yy, mm, dd = int(rc[0:2]), int(rc[2:4]), int(rc[4:6])
    if len(rc) == 9:
        year = 1900 + yy
        if year >= p["nine_digit_before_year"]:
            return {"valid": False,
                    "reason": f"9-digit birth numbers exist only before {p['nine_digit_before_year']}"}
    else:
        mod = p["modulus"]
        if int(rc) % mod != 0:
            legacy = (p.get("allow_legacy_check_digit_zero") and int(rc[:9]) % mod == 10
                      and rc[9] == "0")
            if not legacy:
                return {"valid": False, "reason": f"10-digit birth number is not divisible by {mod}"}
        year = 1900 + yy if yy >= p["century_pivot_yy"] else 2000 + yy
    sex = "M"
    for off in sorted(p["female_month_offsets"], reverse=True):
        if mm > off:
            mm -= off
            sex = "F"
            break
    else:
        for off in sorted(p["male_month_offsets"], reverse=True):
            if mm > off:
                mm -= off
                break
    try:
        bd = date(year, mm, dd)
    except ValueError:
        return {"valid": False, "reason": "birth number encodes an impossible date"}
    return {"valid": True, "reason": "ok", "birth_date": bd.isoformat(), "sex": sex}
