"""Monthly audit-log review (alembic 055): summary contents, sign-off, tenant isolation.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL).
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    TA,
    TB,
    as_a,
    as_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

PERIOD = "2001-02"  # a month no other test writes into
# A tenant of its own per run: the audit log is append-only, so events from earlier runs
# (or other tests) can never be cleaned up and must not be counted.
TR = f"rls_ar_{uuid.uuid4().hex[:8]}"


def _write_chained(events: list[tuple[str, str, str]]) -> None:
    """Insert (action, ISO timestamp, tenant) rows through the real hash-chain writer,
    with back-dated timestamps (the timestamp is not part of the row hash)."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.infrastructure.database.models import AuditLogRecord
    from app.infrastructure.database.repositories import next_audit_chain_link
    from app.infrastructure.tls import db_async_connect_args

    async def run():
        url = OWNER_URL.split("?", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
        eng = create_async_engine(url, connect_args=db_async_connect_args())
        async with AsyncSession(eng) as s:
            for action, ts, tenant, details in events:
                seq, prev, row = await next_audit_chain_link(
                    s, action=action, entity_type="test", entity_id="x", actor="rls_reader",
                    details=details, tenant_id=tenant,
                )
                s.add(AuditLogRecord(
                    id=str(uuid.uuid4()), action=action, entity_type="test", entity_id="x",
                    actor="rls_reader", actor_display="rls_reader", client_ip="203.0.113.9",
                    tenant_id=tenant, details=details, seq=seq, prev_hash=prev, row_hash=row,
                    timestamp=datetime.fromisoformat(ts),
                ))
                await s.flush()
            await s.commit()
        await eng.dispose()

    asyncio.new_event_loop().run_until_complete(run())


@pytest.fixture(scope="module")
def tenant_admin(seeded):
    import json

    from app.domain.permissions import SYSTEM_ROLE_PERMISSIONS
    from tests.rls.test_tenant_isolation import _auth

    admin_id = str(uuid.uuid4())
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO tenants (id, name, slug, is_active, status, plan, features) "
            "VALUES (:id, :id, :id, true, 'active', 'starter', '[]')"
        ), {"id": TR})
        for role_name, perms in SYSTEM_ROLE_PERMISSIONS.items():
            c.execute(sa.text(
                "INSERT INTO roles (id, tenant_id, name, permissions, is_system) "
                "VALUES (:id, :t, :n, CAST(:p AS json), true)"
            ), {"id": str(uuid.uuid4()), "t": TR, "n": role_name, "p": json.dumps(perms)})
        c.execute(sa.text(
            "INSERT INTO users (id, username, email, hashed_password, full_name, role, tenant_id, "
            "is_active, is_platform_admin, is_platform_operator, totp_enabled, created_at, updated_at) "
            "VALUES (:id, :n, :e, 'x', :n, 'admin', :t, true, false, false, false, now(), now())"
        ), {"id": admin_id, "n": f"{TR}_admin", "e": f"{TR}@example.test", "t": TR})
    engine.dispose()
    return _auth(admin_id, f"{TR}_admin", "admin", TR)


@pytest.fixture(scope="module")
def period_events(tenant_admin):
    _write_chained([
        ("login_failed", "2001-02-03T10:00:00+00:00", TR, {}),
        ("login_failed", "2001-02-03T10:01:00+00:00", TR, {}),
        ("account_locked", "2001-02-03T10:02:00+00:00", TR, {}),
        ("break_glass_invoked", "2001-02-10T12:00:00+00:00", TR,
         {"reason": "ED trauma, consultant unreachable"}),
        ("phi_accessed", "2001-02-12T14:00:00+00:00", TR, {}),   # Monday afternoon: in hours
        ("phi_accessed", "2001-02-13T02:30:00+00:00", TR, {}),   # 02:30: after hours
        ("phi_accessed", "2001-02-17T11:00:00+00:00", TR, {}),   # Saturday: after hours
        ("result_exported", "2001-02-20T09:00:00+00:00", TR, {}),
        ("login_failed", "2001-02-21T09:00:00+00:00", TA, {}),   # other tenant: not counted
        ("login_failed", "2001-03-01T00:00:00+00:00", TR, {}),   # next period: not counted
    ])
    return tenant_admin


def test_summary_counts_the_period_for_this_tenant_only(client, period_events):
    r = client.post("/api/admin/audit-reviews", headers=period_events, json={"period": PERIOD})
    assert r.status_code == 201, r.text
    s = r.json()["summary"]
    assert s["failed_sign_ins"]["total"] == 2
    assert s["failed_sign_ins"]["top_client_ips"][0] == {"key": "203.0.113.9", "count": 2}
    assert s["top_readers"] == [{"key": "rls_reader", "count": 3}]
    assert len(s["lockouts"]) == 1
    assert s["emergency_access"][0]["details"]["reason"].startswith("ED trauma")
    assert s["phi_reads"] == 3 and s["after_hours_phi_reads"] == 2
    assert s["disclosures"] == {"result_exported": 1}
    assert r.json()["findings_count"] == 2  # lockout + break-glass


def test_generation_is_idempotent(client, period_events):
    first = client.post("/api/admin/audit-reviews", headers=period_events, json={"period": PERIOD}).json()
    again = client.post("/api/admin/audit-reviews", headers=period_events, json={"period": PERIOD}).json()
    assert first["id"] == again["id"]


def test_mark_reviewed_is_recorded_and_audited(client, period_events):
    review = client.post("/api/admin/audit-reviews", headers=period_events, json={"period": PERIOD}).json()
    r = client.post(f"/api/admin/audit-reviews/{review['id']}/review", headers=period_events,
                    json={"notes": "Lockout was a forgotten password; break-glass justified."})
    assert r.status_code == 200, r.text
    assert r.json()["reviewed_by_username"] == f"{TR}_admin" and r.json()["reviewed_at"]
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        n = c.execute(sa.text("SELECT count(*) FROM audit_log WHERE action = 'audit_reviewed' "
                              "AND entity_id = :i"), {"i": review["id"]}).scalar()
    engine.dispose()
    assert n == 1


def test_other_tenant_cannot_see_the_review(client, as_a, period_events):
    review = client.post("/api/admin/audit-reviews", headers=period_events, json={"period": PERIOD}).json()
    assert client.get(f"/api/admin/audit-reviews/{review['id']}", headers=as_a).status_code == 404
    ids = {x["id"] for x in client.get("/api/admin/audit-reviews", headers=as_a).json()["reviews"]}
    assert review["id"] not in ids


def test_bad_period_is_rejected(client, as_b):
    assert client.post("/api/admin/audit-reviews", headers=as_b, json={"period": "2001-13"}).status_code == 422
    assert client.post("/api/admin/audit-reviews", headers=as_b, json={"period": "2999-01"}).status_code == 422
