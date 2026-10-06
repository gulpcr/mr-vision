"""Audit controls (HIPAA 164.312(b), 164.316(b)(2)): append-only log, read auditing,
one unbroken hash chain across every writer.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL),
plus Redis for the read-audit de-duplication.
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from tests.rls.test_login_hardening import _bearer, _login, _make_user
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_B,
    TB,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")


def _phi_reads(user_id: str, route: str) -> int:
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        n = c.execute(sa.text(
            "SELECT count(*) FROM audit_log WHERE action = 'phi_accessed' AND actor_id = :u "
            "AND details->>'route' = :r"
        ), {"u": user_id, "r": route}).scalar()
    engine.dispose()
    return int(n)


def _user_id(username: str) -> str:
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        uid = c.execute(sa.text("SELECT id FROM users WHERE username = :u"), {"u": username}).scalar()
    engine.dispose()
    return uid


@pytest.fixture(scope="module")
def audit_row(client, seeded):
    """A fresh database has an empty audit_log, where the statements below would match
    no row and so never reach the trigger: sign in once to write a chained entry."""
    _login(client, _make_user())


@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET action = 'tampered' WHERE seq = (SELECT min(seq) FROM audit_log)",
    "DELETE FROM audit_log WHERE seq = (SELECT min(seq) FROM audit_log)",
    "TRUNCATE audit_log",
])
def test_audit_log_is_append_only_even_for_the_owner(seeded, audit_row, statement):
    engine = sa.create_engine(OWNER_URL)
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with engine.begin() as c:
            c.execute(sa.text(statement))
    engine.dispose()


def test_retention_purge_cannot_touch_rows_younger_than_six_years(seeded, audit_row):
    engine = sa.create_engine(OWNER_URL)
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with engine.begin() as c:
            c.execute(sa.text("SET LOCAL app.audit_retention_purge = 'on'"))
            c.execute(sa.text("DELETE FROM audit_log WHERE seq = (SELECT max(seq) FROM audit_log)"))
    engine.dispose()


def test_app_role_has_no_update_or_delete_privilege(seeded):
    app_user = os.environ.get("POSTGRES_APP_USER", "")
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        privs = set(c.execute(sa.text(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'audit_log' AND grantee = :r"
        ), {"r": app_user}).scalars())
    engine.dispose()
    assert {"SELECT", "INSERT"} <= privs
    assert not privs & {"UPDATE", "DELETE", "TRUNCATE"}


def test_patient_data_reads_are_audited_once_per_window(client, seeded):
    username = _make_user()
    headers = _bearer(_login(client, username))
    uid = _user_id(username)
    assert client.get("/api/studies", headers=headers).status_code == 200
    assert client.get("/api/studies", headers=headers).status_code == 200
    assert _phi_reads(uid, "studies.list") == 1
    assert client.get(f"/api/studies/{STUDY_B}", headers=headers).status_code == 200
    assert _phi_reads(uid, "studies") == 1


def test_viewer_image_access_is_audited(client, seeded):
    username = _make_user()
    login = _login(client, username)
    uid = _user_id(username)
    viewer = login.cookies.get("mrv_viewer")
    client.cookies.clear()
    client.cookies.set("mrv_viewer", viewer, path="/")
    r = client.get("/api/internal/dicomweb-authz", headers={
        "X-Original-URI": f"/dicom-web/studies/{STUDY_B}/series", "X-Original-Method": "GET",
    })
    assert r.status_code == 204
    assert _phi_reads(uid, "viewer.dicomweb") == 1


def test_every_writer_extends_one_valid_chain(client, seeded):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.audit_integrity_service import AuditIntegrityService

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        unchained_recent = c.execute(sa.text(
            "SELECT count(*) FROM audit_log WHERE seq IS NULL AND action IN "
            "('phi_accessed', 'login_failed', 'user_logout', 'password_changed')"
        )).scalar()
    engine.dispose()
    assert unchained_recent == 0

    async def verify():
        from app.infrastructure.tls import db_async_connect_args

        # asyncpg takes TLS as connect_args, not libpq's ?sslmode= query parameters.
        url = OWNER_URL.split("?", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
        eng = create_async_engine(url, connect_args=db_async_connect_args())
        async with async_sessionmaker(eng)() as s:
            result = await AuditIntegrityService(s).verify_chain()
        await eng.dispose()
        return result

    result = asyncio.new_event_loop().run_until_complete(verify())
    assert result["valid"], result
