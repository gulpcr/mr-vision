from __future__ import annotations

"""National Provider Identifier (45 CFR 162.406, 162.410): 10 digits whose last digit is a
Luhn check digit computed over the number prefixed with the issuer code 80840 (the NPI
Final Rule check-digit algorithm). Pure function, no I/O."""

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
_PREFIX = "80840"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_npi(value: str | None) -> bool:
    """True for a well-formed NPI: 10 digits, first digit 1 or 2, valid check digit."""
    npi = (value or "").strip()
    if len(npi) != 10 or not npi.isdigit() or npi[0] not in "12":
        return False
    return _luhn_ok(_PREFIX + npi)
