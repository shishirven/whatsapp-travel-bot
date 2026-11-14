"""Normalise phone numbers to the digits WhatsApp uses.

WhatsApp wa_ids are E.164 without the leading plus: country code plus the
national number, digits only. Australian mobiles and landlines are the common
case in a client CSV (`0412 345 678` becomes `61412345678`). A number that
already carries a country code (`+1 555…`, `0061…`) keeps that country code.
"""

from __future__ import annotations

import re

# National significant numbers that start with these digits once the trunk 0
# is removed: 2/3 landline, 4 mobile, 7/8 landline.
_AU_NATIONAL_FIRST = set("23478")


def normalise_au_phone(raw: object) -> str | None:
    """Return WhatsApp digits, or None when the value is not a phone number."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits = digits[2:]
        international = True
    if not digits:
        return None
    # +61 (0)412… puts a trunk zero back into the international form.
    if digits.startswith("610") and len(digits) >= 12:
        digits = "61" + digits[3:]
    if digits.startswith("61") and len(digits) in {11, 12}:
        return digits
    if international:
        if 8 <= len(digits) <= 15:
            return digits
        return None
    if digits.startswith("0") and len(digits) == 10:
        return "61" + digits[1:]
    if len(digits) == 9 and digits[0] in _AU_NATIONAL_FIRST:
        return "61" + digits
    return None
