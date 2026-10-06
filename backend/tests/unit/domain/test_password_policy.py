"""Password policy (NIST 800-63B style): length + blocklist + no username."""
from __future__ import annotations

from app.domain.password_policy import password_problems


def test_long_uncommon_password_is_accepted():
    assert password_problems("correct horse battery staple", "anas") == []


def test_too_short_is_rejected():
    assert any("12 characters" in p for p in password_problems("Short1!", "x"))


def test_common_passwords_rejected_even_with_suffix():
    for pw in ("changeme_in_production", "Password123456!", "admin123admin123"[:8] + "!!!!!!!!"):
        assert password_problems(pw, "someone"), pw
    assert password_problems("Radiologist2026!", "someone")


def test_username_inside_password_rejected():
    assert any("username" in p for p in password_problems("dr-khan-secure-2026", "dr-khan"))


def test_repeated_single_character_rejected():
    assert password_problems("aaaaaaaaaaaaaaa", "x")


def test_configurable_minimum():
    assert password_problems("sixteen-chars-ok", "x", min_length=20)
    assert not password_problems("sixteen-chars-ok", "x", min_length=12)
