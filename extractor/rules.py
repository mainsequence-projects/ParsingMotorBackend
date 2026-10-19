"""Czech birth number (rodné číslo) parsing, parameterised by rules.yaml (RC_VALID.params)."""
from __future__ import annotations

from datetime import date

DEFAULT_RC_PARAMS = {
    "modulus": 11, "lengths": [9, 10], "nine_digit_before_year": 1954, "century_pivot_yy": 54,
    "female_month_offsets": [50, 70], "male_month_offsets": [20],
    "allow_legacy_check_digit_zero": True,
    "extra_offsets_from_year": 2004,       # +20 (M) / +70 (F) only exist since 2004
    "legacy_check_digit_until_year": 1985,  # remainder-10 -> check digit 0 only until 1985
    "reject_future_dates": True,
}


def parse_birth_number(rc: str, params: dict | None = None, today: date | str | None = None) -> dict:
    """Validate a digits-only rodné číslo. Returns {valid, reason, birth_date?, sex?}.

    `today` is the reference date for the "not in the future" check (the run date, so a
    re-evaluation of an old run gives the same answer)."""
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
        year = 1900 + yy if yy >= p["century_pivot_yy"] else 2000 + yy
        if int(rc) % mod != 0:
            legacy = (p.get("allow_legacy_check_digit_zero") and int(rc[:9]) % mod == 10
                      and rc[9] == "0" and year <= p.get("legacy_check_digit_until_year", 9999))
            if not legacy:
                return {"valid": False, "reason": f"10-digit birth number is not divisible by {mod}"}
    sex, offset = "M", 0
    for off in sorted(p["female_month_offsets"], reverse=True):
        if mm > off:
            mm, sex, offset = mm - off, "F", off
            break
    else:
        for off in sorted(p["male_month_offsets"], reverse=True):
            if mm > off:
                mm, offset = mm - off, off
                break
    extra = {o for o in p["male_month_offsets"]} | {o for o in p["female_month_offsets"] if o != min(p["female_month_offsets"])}
    if offset in extra and year < p.get("extra_offsets_from_year", 0):
        return {"valid": False,
                "reason": f"month offset +{offset} only exists since {p.get('extra_offsets_from_year')}"}
    try:
        bd = date(year, mm, dd)
    except ValueError:
        return {"valid": False, "reason": "birth number encodes an impossible date"}
    ref = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
    if p.get("reject_future_dates", True) and bd > ref:
        return {"valid": False, "reason": f"birth number encodes a future date ({bd.isoformat()})"}
    return {"valid": True, "reason": "ok", "birth_date": bd.isoformat(), "sex": sex}
