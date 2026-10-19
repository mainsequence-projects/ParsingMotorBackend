"""Deterministic business rules (Czech birth number etc.)."""
from datetime import date


def parse_birth_number(rc: str):
    """Validate Czech rodne cislo (digits only). Returns dict(valid, reason, birth_date, sex)."""
    if not rc or not rc.isdigit() or len(rc) not in (9, 10):
        return {"valid": False, "reason": "birth number must have 9 or 10 digits"}
    yy, mm, dd = int(rc[0:2]), int(rc[2:4]), int(rc[4:6])
    if len(rc) == 9:
        year = 1900 + yy
        if year >= 1954:
            return {"valid": False, "reason": "9-digit birth numbers exist only before 1954"}
    else:
        if int(rc) % 11 != 0:
            # legacy rule: if first 9 digits mod 11 == 10, check digit is 0
            if not (int(rc[:9]) % 11 == 10 and rc[9] == "0"):
                return {"valid": False, "reason": "10-digit birth number is not divisible by 11"}
        year = 1900 + yy if yy >= 54 else 2000 + yy
    sex = "M"
    if mm > 70:
        mm -= 70; sex = "F"
    elif mm > 50:
        mm -= 50; sex = "F"
    elif mm > 20:
        mm -= 20
    try:
        bd = date(year, mm, dd)
    except ValueError:
        return {"valid": False, "reason": "birth number encodes an impossible date"}
    return {"valid": True, "reason": "ok", "birth_date": bd.isoformat(), "sex": sex}
