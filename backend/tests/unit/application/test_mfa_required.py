"""mfa_required_for: role list + platform-privileged switch."""
from __future__ import annotations

from types import SimpleNamespace

import app.infrastructure.auth.principal as principal


def _settings(roles: str, platform: bool):
    return SimpleNamespace(mfa_required_roles=roles, mfa_required_for_platform=platform)


def test_listed_roles_require_mfa(monkeypatch):
    monkeypatch.setattr(principal, "get_settings", lambda: _settings("admin, Radiologist", False))
    assert principal.mfa_required_for("admin", False)
    assert principal.mfa_required_for("radiologist", False)
    assert not principal.mfa_required_for("viewer", False)


def test_platform_privileged_only_when_enabled(monkeypatch):
    monkeypatch.setattr(principal, "get_settings", lambda: _settings("", True))
    assert principal.mfa_required_for("viewer", True)
    monkeypatch.setattr(principal, "get_settings", lambda: _settings("", False))
    assert not principal.mfa_required_for("viewer", True)


def test_empty_policy_requires_nothing(monkeypatch):
    monkeypatch.setattr(principal, "get_settings", lambda: _settings("", False))
    assert not principal.mfa_required_for("admin", True)


def test_star_requires_mfa_for_every_role(monkeypatch):
    monkeypatch.setattr(principal, "get_settings", lambda: _settings("*", False))
    for role in ("admin", "radiologist", "doctor", "technician", "receptionist", "viewer", "custom-role"):
        assert principal.mfa_required_for(role, False), role
