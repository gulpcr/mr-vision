"""Sessions (HIPAA 164.312(a)(2)(iii), (d)): short access tokens, rotating refresh
cookie, reuse detection, server-enforced idle logoff, revocation reaching the viewer.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL).
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from tests.rls.test_login_hardening import PASSWORD, _bearer, _login, _make_user
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_A,
    TB,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

REFRESH = "mrv_refresh"


def _refresh_cookie(resp) -> str:
    value = resp.cookies.get(REFRESH)
    assert value, "sign-in must set the refresh cookie"
    return value


def _refresh(client, token: str):
    client.cookies.clear()
    client.cookies.set(REFRESH, token, path="/api/auth")
    return client.post("/api/auth/refresh")


def test_refresh_rotates_and_detects_reuse(client, seeded):
    username = _make_user()
    first = _refresh_cookie(_login(client, username))

    r1 = _refresh(client, first)
    assert r1.status_code == 200 and r1.json()["access_token"]
    second = _refresh_cookie(r1)
    assert second != first
    assert client.get("/api/studies", headers=_bearer(r1)).status_code == 200

    # Replayed inside the race grace window: refused, but the session survives.
    assert _refresh(client, first).json()["reason"] == "rotated_recently"
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "UPDATE refresh_sessions SET rotated_at = rotated_at - interval '1 minute' "
            "WHERE rotated_at IS NOT NULL AND user_id = (SELECT id FROM users WHERE username = :u)"
        ), {"u": username})
    engine.dispose()
    replay = _refresh(client, first)  # the rotated token is presented again, later
    assert replay.status_code == 401
    assert _refresh(client, second).status_code == 401  # whole family revoked
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        hits = c.execute(sa.text(
            "SELECT count(*) FROM audit_log WHERE action = 'session_reuse_detected' AND entity_id = "
            "(SELECT id FROM users WHERE username = :u)"
        ), {"u": username}).scalar()
    engine.dispose()
    assert hits == 1


def test_idle_session_cannot_be_refreshed(client, seeded):
    username = _make_user()
    token = _refresh_cookie(_login(client, username))
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "UPDATE refresh_sessions SET last_used_at = now() - interval '20 minutes' "
            "WHERE user_id = (SELECT id FROM users WHERE username = :u)"
        ), {"u": username})
    engine.dispose()
    r = _refresh(client, token)
    assert r.status_code == 401 and r.json()["reason"] == "idle"


def test_logout_ends_the_refresh_session(client, seeded):
    username = _make_user()
    login = _login(client, username)
    token = _refresh_cookie(login)
    client.cookies.clear()
    client.cookies.set(REFRESH, token, path="/api/auth")
    assert client.post("/api/auth/logout", headers=_bearer(login)).status_code == 204
    assert _refresh(client, token).status_code == 401


def test_password_change_ends_other_sessions(client, seeded):
    username = _make_user()
    other_device = _refresh_cookie(_login(client, username))
    this_device = _login(client, username)
    changed = client.post("/api/auth/change-password", headers=_bearer(this_device), json={
        "current_password": PASSWORD, "new_password": "another long passphrase 77",
    })
    assert changed.status_code == 200
    r = _refresh(client, other_device)
    assert r.status_code == 401 and r.json()["reason"] == "token_version"


def test_access_tokens_are_short_lived(client, seeded):
    from app.application.auth_service import AuthService

    username = _make_user()
    payload = AuthService(session=None).decode_token(_login(client, username).json()["access_token"])
    assert payload["exp"] - payload["iat"] <= 15 * 60 + 5


def test_revoked_sessions_lose_viewer_access(client, seeded):
    from app.application.auth_service import AuthService
    from app.infrastructure.auth.principal import invalidate_principal

    username = _make_user()
    login = _login(client, username)
    viewer = login.cookies.get("mrv_viewer")
    assert viewer
    claims = AuthService(session=None).decode_token(viewer)
    assert "tv" in claims

    def authz():
        client.cookies.clear()
        client.cookies.set("mrv_viewer", viewer, path="/")
        return client.get("/api/internal/dicomweb-authz", headers={
            "X-Original-URI": "/dicom-web/studies", "X-Original-Method": "GET",
        })

    assert authz().status_code in (204, 403)  # authenticated (403 = not this tenant's study)
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text("UPDATE users SET token_version = token_version + 1 WHERE username = :u"),
                  {"u": username})
    engine.dispose()
    invalidate_principal(claims["sub"])
    assert authz().status_code == 401
