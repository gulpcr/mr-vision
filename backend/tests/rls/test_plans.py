"""Plans (alembic 048): superadmin-managed plans with use cases, a permission ceiling and a
default seat limit; tenant creation/editing by plan; invitation expiry; AI-model registry
restricted to superadmins.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL is set).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_B,
    TB,
    _auth,
    as_b,
    as_platform,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

SUFFIX = uuid.uuid4().hex[:6]


def _owner_exec(sql: str, params: dict | None = None):
    import sqlalchemy as sa

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        rows = c.execute(sa.text(sql), params or {})
        out = rows.fetchall() if rows.returns_rows else None
    engine.dispose()
    return out


def _invalidate():
    from app.infrastructure.auth.principal import invalidate_all_principals
    from app.infrastructure.tenant.entitlements import invalidate_entitlements

    invalidate_entitlements()
    invalidate_all_principals()


def test_plans_are_superadmin_only(client, as_b, as_platform):
    assert client.get("/api/admin/plans", headers=as_b).status_code == 403
    assert client.post("/api/admin/plans", headers=as_b,
                       json={"name": "x", "display_name": "X"}).status_code == 403
    plans = client.get("/api/admin/plans", headers=as_platform).json()
    assert any(p["name"] == "starter" for p in plans)
    catalog = client.get("/api/admin/plans/catalog", headers=as_platform).json()
    assert "study.view" in {p["key"] for p in catalog["permissions"]} and catalog["usecases"]


def test_tenant_created_on_plan_gets_its_default_seat_limit(client, as_platform):
    key = f"pro-{SUFFIX}"
    r = client.post("/api/admin/plans", headers=as_platform, json={
        "name": key, "display_name": "Pro", "default_max_users": 7,
    })
    assert r.status_code == 201, r.text
    slug = f"seat-{SUFFIX}"
    r = client.post("/api/admin/tenants", headers=as_platform, json={
        "name": "Seat Test", "slug": slug, "plan": key,
        "admin_username": "seatadmin", "admin_email": f"seat{SUFFIX}@example.com",
    })
    assert r.status_code == 201, r.text
    assert r.json()["max_users"] == 7 and r.json()["invite_expires_in_hours"] == 3
    tenant = client.get(f"/api/admin/tenants/{slug}", headers=as_platform).json()
    assert tenant["max_users"] == 7 and tenant["plan"] == key          # visible when editing
    # An explicit seat limit wins over the plan default.
    r = client.post("/api/admin/tenants", headers=as_platform, json={
        "name": "Seat Test 2", "slug": f"seat2-{SUFFIX}", "plan": key, "max_users": 2,
        "admin_username": "seatadmin2", "admin_email": f"seat2{SUFFIX}@example.com",
    })
    assert r.json()["max_users"] == 2
    # Unknown plan is rejected.
    r = client.post("/api/admin/tenants", headers=as_platform, json={
        "name": "Bad", "slug": f"bad-{SUFFIX}", "plan": "no-such-plan",
        "admin_username": "badadmin", "admin_email": f"bad{SUFFIX}@example.com",
    })
    assert r.status_code in (400, 409, 422)
    # A plan in use cannot be deleted.
    assert client.delete(f"/api/admin/plans/{key}", headers=as_platform).status_code == 409
    for s in (slug, f"seat2-{SUFFIX}"):
        assert client.post(f"/api/admin/platform/tenants/{s}/purge", params={"confirm": s},
                           headers=as_platform).status_code == 200


def test_invitation_links_expire_after_three_hours(client, as_b):
    r = client.post("/api/auth/users/invite", headers=as_b, json={
        "username": f"ttl{SUFFIX}", "email": f"ttl{SUFFIX}@example.com", "role": "viewer",
    })
    assert r.status_code == 201, r.text
    assert r.json()["expires_in_hours"] == 3
    (expires,) = _owner_exec("SELECT invitation_expires_at FROM users WHERE username = :u AND tenant_id = :t",
                             {"u": f"ttl{SUFFIX}", "t": TB})[0]
    hours = (expires - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 2.9 < hours <= 3.0
    _owner_exec("DELETE FROM users WHERE username = :u AND tenant_id = :t", {"u": f"ttl{SUFFIX}", "t": TB})


def test_each_invitation_gets_a_distinct_link(client, as_b):
    links = []
    for i in range(2):
        r = client.post("/api/auth/users/invite", headers=as_b, json={
            "username": f"dist{i}{SUFFIX}", "email": f"dist{i}{SUFFIX}@example.com", "role": "viewer",
        })
        links.append(r.json()["invite_link"])
    assert links[0] != links[1]
    _owner_exec("DELETE FROM users WHERE username LIKE :u AND tenant_id = :t", {"u": f"dist%{SUFFIX}", "t": TB})


def test_plan_permission_ceiling_caps_even_the_tenant_admin(client, as_b, as_platform):
    key = f"basic-{SUFFIX}"
    assert client.post("/api/admin/plans", headers=as_platform, json={
        "name": key, "display_name": "Basic",
        "permissions": ["study.view", "dashboard.view", "user.manage"],
    }).status_code == 201
    assert client.put(f"/api/admin/tenants/{TB}/plan", headers=as_platform, json={"plan": key}).status_code == 200
    try:
        _invalidate()
        me = client.get("/api/auth/me/permissions", headers=as_b).json()
        assert set(me["permissions"]) == {"study.view", "dashboard.view", "user.manage"}
        assert client.get("/api/studies", headers=as_b).status_code == 200            # in plan
        assert client.get("/api/auth/users", headers=as_b).status_code == 200         # in plan
        assert client.put("/api/tenant/settings", json={"timezone": "UTC"},
                          headers=as_b).status_code == 403                             # capped
        catalog = client.get("/api/roles/permissions", headers=as_b)
        # role.manage is not in the plan either → the role editor itself is capped
        assert catalog.status_code == 200
        flags = {p["key"]: p["in_plan"] for p in catalog.json()["permissions"]}
        assert flags["study.view"] is True and flags["settings.manage"] is False
    finally:
        client.put(f"/api/admin/tenants/{TB}/plan", headers=as_platform, json={"plan": "starter"})
        _invalidate()
        client.delete(f"/api/admin/plans/{key}", headers=as_platform)
    assert client.put("/api/tenant/settings", json={"timezone": "UTC"}, headers=as_b).status_code == 200


def test_plan_use_cases_limit_ai_jobs(client, as_b, as_platform):
    key = f"brainonly-{SUFFIX}"
    assert client.post("/api/admin/plans", headers=as_platform, json={
        "name": key, "display_name": "Brain only", "usecases": ["brain_mri"],
    }).status_code == 201
    client.put(f"/api/admin/tenants/{TB}/plan", headers=as_platform, json={"plan": key})
    try:
        _invalidate()
        r = client.post(f"/api/studies/{STUDY_B}/jobs", headers=as_b, json={"usecase_names": ["abdomen_ct"]})
        assert r.status_code == 403 and "abdomen_ct" in r.text
    finally:
        client.put(f"/api/admin/tenants/{TB}/plan", headers=as_platform, json={"plan": "starter"})
        _invalidate()
        client.delete(f"/api/admin/plans/{key}", headers=as_platform)


def test_ai_model_registry_is_superadmin_only(client, as_b, as_platform):
    assert client.get("/api/admin/usecases", headers=as_b).status_code == 403
    assert client.get("/api/admin/usecases", headers=as_platform).status_code == 200
