"""TEC-08: NPI check-digit validation (Luhn over 80840 + NPI)."""
from __future__ import annotations

import pytest

from app.domain.npi import is_valid_npi


@pytest.mark.parametrize("npi", ["1234567893", "1245319599", "2000000002"])
def test_valid_npis(npi):
    assert is_valid_npi(npi)


@pytest.mark.parametrize("npi", [
    "1234567890",      # wrong check digit
    "123456789",       # 9 digits
    "12345678931",     # 11 digits
    "3234567893",      # must start with 1 or 2
    "12345 67893", "", None, "ABCDEFGHIJ",
])
def test_invalid_npis(npi):
    assert not is_valid_npi(npi)
