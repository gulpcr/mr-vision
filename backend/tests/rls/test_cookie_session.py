"""Access token as an httpOnly cookie (never in script-readable storage) + CSRF header.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL).
"""
from __future__ import annotations

import pytest

from tests.rls.test_login_hardening import _login, _make_user
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

ACCESS = "mrv_access"
CSRF = {"X-Requested-With": "mrcv"}


def _cookie_only(client, resp) -> str:
    token = resp.cookies.get(ACCESS)
    assert token, "sign-in must set the httpOnly access cookie"
    client.cookies.clear()
    client.cookies.set(ACCESS, token)
    return token


def test_sign_in_sets_httponly_cookie_and_expiry(client, seeded):
    resp = _login(client, _make_user())
    header = "; ".join(v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"
                       and v.startswith(ACCESS + "="))
    assert "HttpOnly" in header and "SameSite=strict" in header.replace("Strict", "strict")
    assert isinstance(resp.json()["expires_at"], int)


def test_cookie_authenticates_reads(client, seeded):
    _cookie_only(client, _login(client, _make_user()))
    assert client.get("/api/studies").status_code == 200


def test_cookie_write_without_csrf_header_is_refused(client, seeded):
    _cookie_only(client, _login(client, _make_user()))
    r = client.post("/api/auth/logout")
    assert r.status_code == 403 and r.json()["code"] == "csrf"
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 204


def test_logout_clears_the_access_cookie(client, seeded):
    _cookie_only(client, _login(client, _make_user()))
    r = client.post("/api/auth/logout", headers=CSRF)
    assert any(v.startswith(ACCESS + "=") and ("Max-Age=0" in v or "expires=" in v.lower())
               for k, v in r.headers.multi_items() if k.lower() == "set-cookie")


def test_token_can_be_withheld_from_json(client, seeded, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "access_token_in_body", False)
    resp = _login(client, _make_user())
    assert resp.json()["access_token"] is None
    _cookie_only(client, resp)
    assert client.get("/api/studies").status_code == 200
