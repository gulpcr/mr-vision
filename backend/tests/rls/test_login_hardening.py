"""Login hardening (HIPAA 164.312(b)/(d)): lockout, IP throttling, failure auditing,
forced password change, forced MFA enrolment, server-side logout.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL is
set) plus a Redis the app can reach (REDIS_HOST / REDIS_PASSWORD).
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    TB,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

PASSWORD = "Str0ng-Passw0rd!"


def _make_user(role: str = "viewer", **cols) -> str:
    from app.application.auth_service import AuthService

    username = f"lh_{uuid.uuid4().hex[:8]}"
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO users (id, username, email, hashed_password, role, tenant_id, is_active, "
            "status, token_version, must_change_password, created_at, updated_at) VALUES "
            "(:id, :u, :e, :h, :r, :t, true, 'active', 0, :m, now(), now())"
        ), {"id": str(uuid.uuid4()), "u": username, "e": f"{username}@example.com",
            "h": AuthService._hash_password(PASSWORD), "r": role, "t": TB,
            "m": bool(cols.get("must_change_password", False))})
    engine.dispose()
    return username


def _audit_actions(username: str) -> list[str]:
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        rows = c.execute(sa.text(
            "SELECT action FROM audit_log WHERE actor = :u ORDER BY seq NULLS LAST, id"
        ), {"u": username}).scalars().all()
    engine.dispose()
    return list(rows)


def _login(client, username: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password,
                                                "workspace": TB})


def _bearer(resp) -> dict:
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def settings_override():
    from app.config import get_settings
    from app.infrastructure.auth.principal import invalidate_all_principals

    s = get_settings()
    saved: dict = {}

    def set_(**kw):
        for k, v in kw.items():
            saved.setdefault(k, getattr(s, k))
            setattr(s, k, v)
        invalidate_all_principals()

    yield set_
    for k, v in saved.items():
        setattr(s, k, v)
    invalidate_all_principals()


def test_failed_logins_are_audited_and_lock_the_account(client, seeded):
    username = _make_user()
    for _ in range(5):
        assert _login(client, username, "wrong-password-1234").status_code == 401
    locked = _login(client, username)  # even the right password is refused while locked
    assert locked.status_code == 423
    assert int(locked.headers["Retry-After"]) > 0
    actions = _audit_actions(username)
    assert actions.count("login_failed") >= 6  # 5 bad passwords + 1 attempt while locked
    assert "account_locked" in actions


def test_unknown_usernames_are_audited_too(client, seeded):
    ghost = f"ghost_{uuid.uuid4().hex[:8]}"
    assert _login(client, ghost, "whatever-password").status_code == 401
    assert "login_failed" in _audit_actions(ghost)


def test_per_ip_login_throttle(client, seeded, settings_override):
    settings_override(login_ip_attempts_per_minute=2)
    username = _make_user()
    codes = [_login(client, username, "wrong-password-1234").status_code for _ in range(3)]
    assert codes[-1] == 429


def test_forced_password_change_restricts_the_session(client, seeded):
    username = _make_user(must_change_password=True)
    login = _login(client, username)
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    assert "mrv_viewer" not in login.cookies  # no image access yet
    headers = _bearer(login)

    blocked = client.get("/api/studies", headers=headers)
    assert blocked.status_code == 403 and blocked.json()["code"] == "password_change_required"
    perms = client.get("/api/auth/me/permissions", headers=headers).json()
    assert perms["must_change_password"] is True

    weak = client.post("/api/auth/change-password", headers=headers,
                       json={"current_password": PASSWORD, "new_password": "password12345"})
    assert weak.status_code == 400 and weak.json()["detail"]["problems"]
    wrong = client.post("/api/auth/change-password", headers=headers,
                        json={"current_password": "not-it-at-all", "new_password": "a new long passphrase 42"})
    assert wrong.status_code == 400

    ok = client.post("/api/auth/change-password", headers=headers,
                     json={"current_password": PASSWORD, "new_password": "a new long passphrase 42"})
    assert ok.status_code == 200 and ok.json()["must_change_password"] is False
    assert client.get("/api/studies", headers=headers).status_code == 401  # old token revoked
    assert client.get("/api/studies", headers=_bearer(ok)).status_code == 200
    assert "password_changed" in _audit_actions(username)


def test_mfa_required_role_must_enrol_first(client, seeded, settings_override):
    settings_override(mfa_required_roles="radiologist")
    username = _make_user(role="radiologist")
    login = _login(client, username)
    assert login.status_code == 200 and login.json()["mfa_enrollment_required"] is True
    headers = _bearer(login)
    blocked = client.get("/api/studies", headers=headers)
    assert blocked.status_code == 403 and blocked.json()["code"] == "mfa_enrollment_required"
    assert client.post("/api/auth/mfa/enroll", headers=headers).status_code == 200


def test_logout_revokes_the_token_and_is_audited(client, seeded):
    username = _make_user()
    headers = _bearer(_login(client, username))
    assert client.get("/api/studies", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/studies", headers=headers).status_code == 401
    assert "user_logout" in _audit_actions(username)


def test_failed_mfa_codes_are_audited(client, seeded):
    pyotp = pytest.importorskip("pyotp")
    username = _make_user()
    headers = _bearer(_login(client, username))
    secret = client.post("/api/auth/mfa/enroll", headers=headers).json()["secret"]
    assert client.post("/api/auth/mfa/confirm", headers=headers,
                       json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    step1 = _login(client, username)
    assert step1.json()["mfa_required"] is True
    bad = client.post("/api/auth/mfa/verify", json={"mfa_token": step1.json()["mfa_token"],
                                                    "code": "000000"})
    assert bad.status_code == 401
    assert "mfa_failed" in _audit_actions(username)
