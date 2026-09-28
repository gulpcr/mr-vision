"""End-to-end tenant isolation against a real Postgres with Row-Level Security.

Skipped unless ``RLS_TEST_OWNER_DATABASE_URL`` is set. It needs a database migrated to
head (alembic 044+) and the app configured to connect as the RLS-bound app role
(POSTGRES_HOST / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_APP_USER /
POSTGRES_APP_PASSWORD, AUTH_MODE=jwt). The owner URL is used only to seed fixtures
(owners bypass RLS).

    docker run --rm --network <net> -v $PWD:/app -w /app \\
      -e RLS_TEST_OWNER_DATABASE_URL=postgresql://mri_admin:pw@pg:5432/mri_platform \\
      -e POSTGRES_HOST=pg -e POSTGRES_USER=mri_admin -e POSTGRES_PASSWORD=pw \\
      -e POSTGRES_APP_USER=mrv_app -e POSTGRES_APP_PASSWORD=apppw -e AUTH_MODE=jwt \\
      <backend-image> python -m pytest tests/rls -q

Two tenants (A, B) are seeded with a study, a result, a mammography report and users
each; every assertion checks that tenant B can neither read nor modify tenant A's rows
through the HTTP API, and that the database itself enforces it.
"""
from __future__ import annotations

import os
import uuid

import pytest

OWNER_URL = os.environ.get("RLS_TEST_OWNER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

TA, TB = "rls_tenant_a", "rls_tenant_b"
STUDY_A, STUDY_B = "1.2.826.0.1.9001.1", "1.2.826.0.1.9001.2"


def _uid() -> str:
    return str(uuid.uuid4())


def _run_isolated(coro_factory):
    """Run an async DB check on its own event loop, then drain the shared engine's pool:
    asyncpg connections are bound to the loop that opened them, and the TestClient runs
    the app on a different loop."""
    import asyncio

    from app.infrastructure.database.session import engine

    loop = asyncio.new_event_loop()
    try:
        # Drop (without closing) connections another loop may have left in the pool.
        loop.run_until_complete(engine.dispose(close=False))
        return loop.run_until_complete(coro_factory())
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()


@pytest.fixture(scope="module")
def seeded():
    import sqlalchemy as sa

    engine = sa.create_engine(OWNER_URL)
    ids = {
        "admin_a": _uid(), "rad_a": _uid(),
        "admin_b": _uid(), "rad_b": _uid(),
        "platform": _uid(),
    }
    with engine.begin() as c:
        for tid in (TA, TB):
            c.execute(sa.text(
                "INSERT INTO tenants (id, name, slug, is_active, status, plan, features) "
                "VALUES (:id, :id, :id, true, 'active', 'starter', '[]') ON CONFLICT (id) DO NOTHING"
            ), {"id": tid})
            # Seeded exactly as provisioning does: the tenant's system roles.
            import json

            from app.domain.permissions import SYSTEM_ROLE_PERMISSIONS

            for role_name, perms in SYSTEM_ROLE_PERMISSIONS.items():
                c.execute(sa.text(
                    "INSERT INTO roles (id, tenant_id, name, permissions, is_system) "
                    "VALUES (:id, :t, :n, CAST(:p AS json), true) ON CONFLICT DO NOTHING"
                ), {"id": _uid(), "t": tid, "n": role_name, "p": json.dumps(perms)})
        users = [
            (ids["admin_a"], "rls_admin_a", "admin", TA, False),
            (ids["rad_a"], "rls_rad_a", "radiologist", TA, False),
            (ids["admin_b"], "rls_admin_b", "admin", TB, False),
            (ids["rad_b"], "rls_rad_b", "radiologist", TB, False),
            (ids["platform"], "rls_platform", "admin", "default", True),
        ]
        for uid, name, role, tid, plat in users:
            c.execute(sa.text(
                "INSERT INTO users (id, username, email, hashed_password, full_name, role, "
                "tenant_id, is_active, is_platform_admin, is_platform_operator, totp_enabled, "
                "created_at, updated_at) VALUES (:id, :n, :e, 'x', :n, :r, :t, true, :p, false, "
                "false, now(), now())"
            ), {"id": uid, "n": name, "e": f"{name}@example.test", "r": role, "t": tid, "p": plat})
        for study, tid in ((STUDY_A, TA), (STUDY_B, TB)):
            c.execute(sa.text(
                "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, "
                "modality, reading_status, created_at, updated_at) "
                "VALUES (:s, :t, 'MRN-SHARED', :n, 'MG', 'unread', now(), now())"
            ), {"s": study, "t": tid, "n": f"Patient {tid}"})
            c.execute(sa.text(
                "INSERT INTO mammography_reports (study_instance_uid, tenant_id, opinion, "
                "created_at, updated_at) VALUES (:s, :t, :o, now(), now())"
            ), {"s": study, "t": tid, "o": f"opinion of {tid}"})
        # Role-default dashboards, as provisioning seeds them.
        from app.domain.dashboards import ROLE_DEFAULT_DASHBOARDS

        for tid in (TA, TB):
            for role_name, spec in ROLE_DEFAULT_DASHBOARDS.items():
                c.execute(sa.text(
                    "INSERT INTO dashboard_layouts (id, tenant_id, name, is_default, role_default, "
                    "widgets, filters) VALUES (:id, :t, :n, true, :r, CAST(:w AS json), '{}') "
                    "ON CONFLICT DO NOTHING"
                ), {"id": _uid(), "t": tid, "n": spec["name"], "r": role_name,
                    "w": json.dumps(spec["widgets"])})
    yield ids
    with engine.begin() as c:
        for table in ("dashboard_versions", "dashboard_layouts", "tenant_dicom_endpoints",
                      "mammography_reports", "studies", "user_roles", "users", "roles",
                      "tenant_settings", "tenant_branding"):
            c.execute(sa.text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"), {"a": TA, "b": TB})
        c.execute(sa.text("DELETE FROM users WHERE username = 'rls_platform'"))
        # audit_log rows are deliberately kept: deleting them would break the hash chain.
        c.execute(sa.text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": TA, "b": TB})
    engine.dispose()


@pytest.fixture(scope="module")
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    from app.infrastructure.database.session import engine

    # Each test module gets its own TestClient (and event loop); drop pooled asyncpg
    # connections bound to a previous module's loop.
    engine.sync_engine.dispose(close=False)
    with TestClient(app) as c:
        yield c
    engine.sync_engine.dispose(close=False)


def _auth(user_id: str, username: str, role: str, tenant_id: str, platform: bool = False) -> dict:
    from app.application.auth_service import AuthService

    token = AuthService(session=None).create_access_token(
        subject=user_id, username=username, role=role, tenant_id=tenant_id,
        is_platform_admin=platform,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def as_a(seeded):
    return _auth(seeded["admin_a"], "rls_admin_a", "admin", TA)


@pytest.fixture(scope="module")
def as_b(seeded):
    return _auth(seeded["admin_b"], "rls_admin_b", "admin", TB)


@pytest.fixture(scope="module")
def as_rad_b(seeded):
    return _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)


@pytest.fixture(scope="module")
def as_platform(seeded):
    return _auth(seeded["platform"], "rls_platform", "admin", "default", platform=True)


def test_db_role_is_rls_bound():
    from app.infrastructure.database.session import rls_enforcement_status

    enforced, role = _run_isolated(rls_enforcement_status)
    assert enforced, f"app connects as {role}, which bypasses RLS — test would prove nothing"


def test_study_list_is_tenant_confined(client, as_a, as_b):
    a = {s["study_instance_uid"] for s in client.get("/api/studies", headers=as_a).json()["studies"]}
    b = {s["study_instance_uid"] for s in client.get("/api/studies", headers=as_b).json()["studies"]}
    assert STUDY_A in a and STUDY_B not in a
    assert STUDY_B in b and STUDY_A not in b


def test_other_tenants_study_is_not_found(client, as_b):
    assert client.get(f"/api/studies/{STUDY_A}", headers=as_b).status_code == 404


def test_cannot_delete_other_tenants_study(client, as_b, as_a):
    assert client.delete(f"/api/studies/{STUDY_A}", headers=as_b).status_code == 404
    assert client.get(f"/api/studies/{STUDY_A}", headers=as_a).status_code == 200


def test_mammography_report_is_tenant_confined(client, as_b):
    body = client.get(f"/api/studies/{STUDY_A}/mammography-report", headers=as_b).json()
    assert body == {}
    own = client.get(f"/api/studies/{STUDY_B}/mammography-report", headers=as_b).json()
    assert own.get("opinion") == f"opinion of {TB}"


def test_reading_workflow_cannot_claim_other_tenants_study(client, seeded):
    headers = _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)
    assert client.post(f"/api/studies/{STUDY_A}/claim", headers=headers).status_code == 404


def test_cannot_change_other_tenants_user(client, seeded, as_b, as_a):
    r = client.put(f"/api/auth/users/{seeded['rad_a']}/role", params={"role": "viewer"}, headers=as_b)
    assert r.status_code == 404
    r = client.delete(f"/api/auth/users/{seeded['rad_a']}", headers=as_b)
    assert r.status_code == 404
    users = {u["id"]: u for u in client.get("/api/auth/users", headers=as_a).json()}
    assert users[seeded["rad_a"]]["role"] == "radiologist"
    assert users[seeded["rad_a"]]["is_active"] is True


def test_user_list_is_tenant_confined(client, seeded, as_b):
    ids = {u["id"] for u in client.get("/api/auth/users", headers=as_b).json()}
    assert seeded["admin_b"] in ids and seeded["admin_a"] not in ids
    assert client.get("/api/auth/users", params={"tenant_id": TA}, headers=as_b).status_code == 403


def test_last_admin_cannot_be_demoted(client, seeded, as_b):
    r = client.put(f"/api/auth/users/{seeded['admin_b']}/role", params={"role": "radiologist"}, headers=as_b)
    assert r.status_code == 409


def test_non_admin_cannot_grant_admin(client, seeded):
    # rad_b lacks user.manage entirely → 403 before the admin-grant check.
    headers = _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)
    r = client.put(f"/api/auth/users/{seeded['rad_b']}/role", params={"role": "admin"}, headers=headers)
    assert r.status_code == 403


def test_in_tenant_role_change_is_audited_and_chain_stays_valid(client, seeded, as_b, as_platform):
    r = client.put(f"/api/auth/users/{seeded['rad_b']}/role", params={"role": "radiologist"}, headers=as_b)
    assert r.status_code == 200, r.text
    verify = client.get("/api/admin/audit/verify", headers=as_platform)
    assert verify.status_code == 200, verify.text
    assert verify.json()["valid"] is True, verify.json()


def test_platform_admin_sees_every_tenant(client, seeded, as_platform):
    tenants = {t["id"] for t in client.get("/api/admin/tenants", headers=as_platform).json()}
    assert {TA, TB} <= tenants
    ids = {u["id"] for u in client.get("/api/auth/users", params={"tenant_id": TB}, headers=as_platform).json()}
    assert seeded["admin_b"] in ids


def test_tenant_admin_cannot_edit_global_routing_rules(client, as_b):
    r = client.put("/api/admin/routing-rules", json={"rules": []}, headers=as_b)
    assert r.status_code == 403


def test_unscoped_app_session_sees_nothing():
    """The fail-closed default: an app-role session with no bound tenant sees no rows."""
    from sqlalchemy import text

    from app.infrastructure.database.session import async_session_factory
    from app.infrastructure.tenant.db_scope import clear_scope, reset_scope

    async def _count() -> int:
        async with async_session_factory() as s:
            return (await s.execute(text("SELECT count(*) FROM studies"))).scalar_one()

    token = clear_scope()
    try:
        assert _run_isolated(_count) == 0
    finally:
        reset_scope(token)


def test_login_works_under_rls_and_token_carries_own_tenant(client, seeded):
    """Login looks the user up before any tenant is known (platform DB scope) — it must
    still work with RLS enforced, and the issued token must carry the user's tenant."""
    import sqlalchemy as sa

    from app.application.auth_service import AuthService

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(
            sa.text("UPDATE users SET hashed_password = :h WHERE id = :id"),
            {"h": AuthService._hash_password("Correct-Horse-9"), "id": seeded["rad_b"]},
        )
    engine.dispose()

    r = client.post("/api/auth/login", json={"username": "rls_rad_b", "password": "Correct-Horse-9"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TB
    studies = client.get(
        "/api/studies", headers={"Authorization": f"Bearer {body['access_token']}"}
    ).json()["studies"]
    assert {s["study_instance_uid"] for s in studies} == {STUDY_B}
