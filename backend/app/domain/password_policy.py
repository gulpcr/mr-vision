"""Password policy (HIPAA 164.308(a)(5)(ii)(D) password management; NIST SP 800-63B).

Length and a blocklist, deliberately no composition rules ("one digit, one symbol"):
NIST found those push people to predictable patterns. Pure function, no I/O.
"""
from __future__ import annotations

import re

MAX_LENGTH = 256

# Most common leaked passwords plus the platform's own historic defaults. Checked
# case-insensitively and after stripping trailing digits/punctuation ("Password123!").
_BLOCKLIST = frozenset({
    "password", "passw0rd", "p@ssw0rd", "p@ssword", "123456789012", "1234567890",
    "qwertyuiop", "qwerty123456", "iloveyou", "letmein", "welcome", "admin", "admin123",
    "administrator", "changeme", "changemenow", "changeme_in_production", "orthanc",
    "radiology", "radiologist", "hospital", "doctor", "medical", "patient", "health",
    "healthcare", "football", "baseball", "dragon", "monkey", "sunshine", "princess",
    "trustno1", "abc123", "abcdefghijkl", "aaaaaaaaaaaa", "superman", "batman",
    "starwars", "whatever", "freedom", "master", "shadow", "michael", "jennifer",
    "computer", "internet", "secret", "summer", "winter", "spring", "autumn",
    "pakistan", "karachi", "lahore", "islamabad", "mrvision", "mr-vision",
})
_TRAILING_NOISE = re.compile(r"[\d\W_]+$")


def password_problems(password: str, username: str = "", min_length: int = 12) -> list[str]:
    """Human-readable reasons ``password`` is not acceptable; empty list if it is."""
    problems: list[str] = []
    if len(password) < min_length:
        problems.append(f"Use at least {min_length} characters.")
    if len(password) > MAX_LENGTH:
        problems.append(f"Use at most {MAX_LENGTH} characters.")
    lowered = password.lower()
    core = _TRAILING_NOISE.sub("", lowered)
    if lowered in _BLOCKLIST or core in _BLOCKLIST:
        problems.append("This password is too common — choose something less predictable.")
    if len(set(password)) <= 2:
        problems.append("Don't repeat the same character.")
    name = (username or "").strip().lower()
    if len(name) >= 3 and name in lowered:
        problems.append("Don't include your username.")
    return problems
