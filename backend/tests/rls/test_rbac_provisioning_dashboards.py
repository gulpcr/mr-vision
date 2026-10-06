"""Phases 3–6: provisioning, per-tenant accounts, invitations, live RBAC, the referring
Doctor's referral scope, dashboards, workspace settings and the operator console.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL is
set). Tenants created here are purged at the end through the platform purge endpoint.
"""
from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_A,
    TA,
    TB,
    _auth,
    as_a,
    as_b,
    as_platform,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

SUFFIX = uuid.uuid4().hex[:6]
DIGITS = str(uuid.uuid4().int)[:8]  # DICOM UIDs are digits and dots only
NEW_SLUG = f"hosp-{SUFFIX}"
PASSWORD = "Str0ng-Passw0rd!"


def _token_from_link(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


def _login(client, username: str, workspace: str | None = None, password: str = PASSWORD):
    body = {"username": username, "password": password}
    if workspace:
        body["workspace"] = workspace
    return client.post("/api/auth/login", json=body)


def _bearer(resp) -> dict:
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── route coverage ───────────────────────────────────────────────────────────

PUBLIC_OR_SELF_AUTH = {
    "/health", "/metrics", "/api/cortex", "/api/auth/login", "/api/auth/register",
    "/api/auth/mfa/verify", "/api/auth/invitations/accept", "/api/tenant/public-branding",
    "/api/auth/refresh",
    "/api/dicom/upload", "/api/orthanc/notify-stable-study", "/api/internal/dicomweb-authz",
    "/api/dicomweb/studies", "/api/dicomweb/studies/{study_uid}/series",
    "/api/dicomweb/studies/{study_uid}/series/{series_uid}/metadata",
    "/api/dicomweb/studies/{study_uid}/metadata",
    "/api/dicomweb/studies/{study_uid}/series/{series_uid}/instances",
    "/api/dicomweb/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}",
    "/api/dicomweb/studies/{study_uid}/series/{series_uid}",
    "/api/dicomweb/studies/{study_uid}",
    "/api/dicomweb/wado",
}
# Any authenticated user (self-service or shared reference data).
AUTHENTICATED_ONLY = {
    "/api/auth/me", "/api/auth/me/permissions", "/api/auth/mfa/enroll", "/api/auth/mfa/confirm",
    "/api/auth/mfa/disable", "/api/auth/viewer-session", "/api/auth/impersonate/stop",
    "/api/auth/change-password", "/api/auth/logout",
    "/api/usecases", "/api/usecases/{usecase_name}/ui-schema",
    "/api/usecases/{usecase_name}/output-schema", "/api/tenant/current",
}


def test_every_route_declares_a_permission():
    """CI guard: a new route without require_permission / platform guard fails here."""
    from fastapi.routing import APIRoute

    from app.main import app

    def guarded(dependant) -> bool:
        for sub in dependant.dependencies:
            name = getattr(sub.call, "__qualname__", "")
            if "check_permission" in name or "require_platform" in name or guarded(sub):
                return True
        return False

    missing = [
        f"{sorted(r.methods)} {r.path}"
        for r in app.routes
        if isinstance(r, APIRoute)
        and r.path not in PUBLIC_OR_SELF_AUTH
        and r.path not in AUTHENTICATED_ONLY
        and not guarded(r.dependant)
    ]
    assert not missing, "routes without a permission guard:\n" + "\n".join(missing)


def test_public_registration_is_disabled(client):
    r = client.post("/api/auth/register", json={
        "username": f"anon{SUFFIX}", "email": f"anon{SUFFIX}@example.com", "password": "whatever123",
    })
    assert r.status_code == 403


# ── provisioning, invitations, per-tenant accounts ───────────────────────────

@pytest.fixture(scope="module")
def new_tenant(client, as_platform):
    r = client.post("/api/admin/tenants", headers=as_platform, json={
        "name": f"Hospital {SUFFIX}", "slug": NEW_SLUG, "plan": "starter", "features": [],
        "admin_username": "hadmin", "admin_email": f"hadmin{SUFFIX}@example.com",
        "called_aet": f"H{SUFFIX}"[:16], "max_users": 4,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["admin_temp_password"] is None and body["admin_invite_link"]
    accept = client.post("/api/auth/invitations/accept", json={
        "token": _token_from_link(body["admin_invite_link"]), "password": PASSWORD,
    })
    assert accept.status_code == 200, accept.text
    login = _login(client, "hadmin", NEW_SLUG)
    assert login.status_code == 200, login.text
    yield {"id": body["id"], "admin": _bearer(login)}
    purge = client.post(f"/api/admin/platform/tenants/{body['id']}/purge",
                        params={"confirm": NEW_SLUG}, headers=as_platform)
    assert purge.status_code == 200, purge.text


def test_provisioning_seeds_roles_dashboards_settings_and_aet(client, new_tenant, as_platform):
    roles = {r["name"] for r in client.get("/api/roles", headers=new_tenant["admin"]).json()}
    assert {"admin", "radiologist", "doctor", "technician", "receptionist", "viewer"} <= roles
    dashboards = client.get("/api/dashboards", headers=new_tenant["admin"]).json()
    assert {d["role_default"] for d in dashboards if d["is_default"]} >= {"admin", "doctor", "radiologist"}
    current = client.get("/api/tenant/current", headers=new_tenant["admin"]).json()
    assert current["branding"]["display_name"] == f"Hospital {SUFFIX}"
    aets = client.get(f"/api/admin/tenants/{new_tenant['id']}/dicom-endpoints", headers=as_platform).json()
    assert [a["called_aet"] for a in aets] == [f"H{SUFFIX}".upper()[:16]]


def test_invited_user_cannot_log_in_before_accepting(client, new_tenant):
    r = client.post("/api/auth/users/invite", headers=new_tenant["admin"], json={
        "username": "pending1", "email": f"pending1{SUFFIX}@example.com", "role": "viewer",
    })
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "invited"
    assert _login(client, "pending1", NEW_SLUG, password="anything-at-all").status_code == 401


def test_invite_requires_existing_role_and_enforces_seat_limit(client, new_tenant):
    bad = client.post("/api/auth/users/invite", headers=new_tenant["admin"], json={
        "username": "nobody", "email": f"nobody{SUFFIX}@example.com", "role": "wizard",
    })
    assert bad.status_code == 409
    # max_users=4: hadmin + pending1 + two more fit, the next one does not.
    for i in range(2):
        ok = client.post("/api/auth/users/invite", headers=new_tenant["admin"], json={
            "username": f"seat{i}", "email": f"seat{i}{SUFFIX}@example.com", "role": "viewer",
        })
        assert ok.status_code == 201, ok.text
    full = client.post("/api/auth/users/invite", headers=new_tenant["admin"], json={
        "username": "seat9", "email": f"seat9{SUFFIX}@example.com", "role": "viewer",
    })
    assert full.status_code == 402


def test_usernames_are_per_workspace(client, new_tenant, as_a):
    # "rls_admin_a" already exists in tenant A; the same username in another workspace is fine.
    import sqlalchemy as sa

    from app.application.auth_service import AuthService

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO users (id, username, email, hashed_password, role, tenant_id, is_active, "
            "status, token_version, created_at, updated_at) VALUES (:id, 'rls_admin_a', :e, :h, "
            "'viewer', :t, true, 'active', 0, now(), now())"
        ), {"id": str(uuid.uuid4()), "e": f"dup{SUFFIX}@example.com",
            "h": AuthService._hash_password(PASSWORD), "t": new_tenant["id"]})
        c.execute(sa.text("UPDATE users SET hashed_password = :h WHERE username = 'rls_admin_a' "
                          "AND tenant_id = :t"), {"h": AuthService._hash_password(PASSWORD), "t": TA})
    engine.dispose()
    assert _login(client, "rls_admin_a").status_code == 409            # ambiguous
    assert _login(client, "rls_admin_a", TA).json()["tenant_id"] == TA
    assert _login(client, "rls_admin_a", NEW_SLUG).json()["tenant_id"] == new_tenant["id"]
    assert _login(client, "rls_admin_a", "no-such-workspace").status_code == 404


# ── live RBAC ────────────────────────────────────────────────────────────────

def test_me_permissions_reflect_the_live_role(client, seeded):
    rad = _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)
    body = client.get("/api/auth/me/permissions", headers=rad).json()
    assert "result.approve" in body["permissions"] and body["is_admin"] is False
    # A role claim in the token is NOT trusted: a token claiming "admin" still gets the
    # user's real (radiologist) permissions.
    forged_role = _auth(seeded["rad_b"], "rls_rad_b", "admin", TB)
    assert client.get("/api/admin/audit", headers=forged_role).status_code == 403


def test_revoking_sessions_invalidates_existing_tokens(client, seeded, as_b):
    headers = _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)
    assert client.get("/api/studies", headers=headers).status_code == 200
    assert client.post(f"/api/auth/users/{seeded['rad_b']}/revoke-sessions", headers=as_b).status_code == 200
    assert client.get("/api/studies", headers=headers).status_code == 401
    # restore token_version 0 for the rest of the module
    import sqlalchemy as sa

    from app.infrastructure.auth.principal import invalidate_principal

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text("UPDATE users SET token_version = 0 WHERE id = :id"), {"id": seeded["rad_b"]})
    engine.dispose()
    invalidate_principal(seeded["rad_b"])


# ── referring Doctor ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def doctor(seeded):
    import sqlalchemy as sa

    doc_id, referred, other = str(uuid.uuid4()), f"1.2.826.0.1.9001.{DIGITS}1", f"1.2.826.0.1.9001.{DIGITS}2"
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO users (id, username, email, hashed_password, role, tenant_id, is_active, "
            "status, token_version, created_at, updated_at) VALUES (:id, :n, :e, 'x', 'doctor', :t, "
            "true, 'active', 0, now(), now())"
        ), {"id": doc_id, "n": f"doc{SUFFIX}", "e": f"doc{SUFFIX}@example.com", "t": TA})
        for uid, ref in ((referred, doc_id), (other, None)):
            c.execute(sa.text(
                "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, modality, "
                "reading_status, referring_user_id, reported_at, created_at, updated_at) VALUES "
                "(:s, :t, 'MRN-D', 'CT', 'reported', :r, now(), now(), now())"
            ), {"s": uid, "t": TA, "r": ref})
    engine.dispose()
    yield {"headers": _auth(doc_id, f"doc{SUFFIX}", "doctor", TA), "referred": referred, "other": other}
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM studies WHERE study_instance_uid IN (:a, :b)"),
                  {"a": referred, "b": other})
        c.execute(sa.text("DELETE FROM dashboard_layouts WHERE owner_id = :id"), {"id": doc_id})
        c.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": doc_id})
    engine.dispose()


def test_doctor_sees_only_referred_studies(client, doctor):
    uids = {s["study_instance_uid"] for s in client.get("/api/studies", headers=doctor["headers"]).json()["studies"]}
    assert uids == {doctor["referred"]}
    assert client.get(f"/api/studies/{doctor['other']}", headers=doctor["headers"]).status_code == 404
    assert client.get(f"/api/studies/{STUDY_A}", headers=doctor["headers"]).status_code == 404
    assert client.get(f"/api/studies/{doctor['referred']}", headers=doctor["headers"]).status_code == 200
    # Doctors cannot do workspace-wide operations.
    assert client.post(f"/api/studies/{doctor['referred']}/claim", headers=doctor["headers"]).status_code == 403


def test_doctor_dashboard_counts_only_referred_studies(client, doctor):
    boards = client.get("/api/dashboards", headers=doctor["headers"]).json()
    mine = next(d for d in boards if d["role_default"] == "doctor")
    data = client.post(f"/api/dashboards/{mine['id']}/data", json={}, headers=doctor["headers"]).json()["data"]
    assert data["kpi"]["data"]["value"] == 1
    assert {r["study_instance_uid"] for r in data["recent"]["data"]["rows"]} == {doctor["referred"]}
    # A widget outside the doctor's permissions cannot be added.
    r = client.post("/api/dashboards", headers=doctor["headers"], json={"name": "x", "widgets": [{
        "id": "w", "type": "radiologist_workload", "title": "t", "config": {},
        "position": {"x": 0, "y": 0, "w": 6, "h": 4}}]})
    assert r.status_code == 403


# ── dashboards ───────────────────────────────────────────────────────────────

def test_role_default_is_read_only_clone_edit_version_restore(client, seeded, as_b):
    rad = _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)
    boards = client.get("/api/dashboards", headers=rad).json()
    default = boards[0]
    assert default["role_default"] == "radiologist" and default["can_edit"] is False
    assert client.put(f"/api/dashboards/{default['id']}", json={"name": "hack"}, headers=rad).status_code == 403

    clone = client.post(f"/api/dashboards/{default['id']}/clone", json={}, headers=rad)
    assert clone.status_code == 201, clone.text
    cid = clone.json()["id"]
    widgets = clone.json()["widgets"][:2]
    upd = client.put(f"/api/dashboards/{cid}", json={"name": "Mine", "widgets": widgets}, headers=rad)
    assert upd.status_code == 200 and len(upd.json()["widgets"]) == 2
    versions = client.get(f"/api/dashboards/{cid}/versions", headers=rad).json()
    assert versions[0]["version"] == 1
    restored = client.post(f"/api/dashboards/{cid}/versions/1/restore", headers=rad).json()
    assert len(restored["widgets"]) == len(default["widgets"])

    data = client.post(f"/api/dashboards/{cid}/data", json={"filters": {"period": "7d"}}, headers=rad).json()["data"]
    assert data and all(v.get("error") is None for v in data.values()), data

    # Tenant B's radiologist dashboards are invisible to tenant A's admin.
    a_admin = _auth(seeded["admin_a"], "rls_admin_a", "admin", TA)
    assert client.get(f"/api/dashboards/{cid}", headers=a_admin).status_code == 404
    # The workspace admin may edit the role default (tailoring it for the whole tenant).
    assert client.put(f"/api/dashboards/{default['id']}", json={"name": "Radiology"}, headers=as_b).status_code == 200
    assert client.delete(f"/api/dashboards/{cid}", headers=rad).status_code == 204


# ── workspace settings / branding ────────────────────────────────────────────

def test_settings_and_branding(client, seeded, as_b):
    rad = _auth(seeded["rad_b"], "rls_rad_b", "radiologist", TB)
    assert client.put("/api/tenant/settings", json={"institution_name": "x"}, headers=rad).status_code == 403
    r = client.put("/api/tenant/settings", headers=as_b,
                   json={"institution_name": "Hospital B Imaging", "signatory_name": "Dr B"})
    assert r.status_code == 200 and r.json()["institution_name"] == "Hospital B Imaging"
    assert client.put("/api/tenant/branding", json={"primary_color": "red"}, headers=as_b).status_code == 422
    assert client.put("/api/tenant/branding", headers=as_b,
                      json={"display_name": "Hosp B", "primary_color": "#112233"}).status_code == 200
    public = client.get("/api/tenant/public-branding", params={"workspace": TB}).json()
    assert public["display_name"] == "Hosp B" and public["primary_color"] == "#112233"
    assert "institution_name" not in public   # public endpoint exposes branding only


# ── operator console ─────────────────────────────────────────────────────────

def test_platform_overview_audit_and_user_search(client, seeded, as_platform, as_b):
    overview = client.get("/api/admin/platform/overview", headers=as_platform).json()
    ids = {t["tenant_id"] for t in overview["tenants"]}
    assert {TA, TB} <= ids and overview["totals"]["tenants"] >= 2
    audit = client.get("/api/admin/platform/audit", params={"tenant_id": TB}, headers=as_platform).json()
    assert audit and all(e["tenant_id"] == TB for e in audit)
    users = client.get("/api/admin/platform/users", params={"q": "rls_"}, headers=as_platform).json()
    assert {u["tenant_id"] for u in users} >= {TA, TB}
    # Tenant admins have no access to the operator console.
    assert client.get("/api/admin/platform/overview", headers=as_b).status_code == 403


def test_purge_requires_matching_confirmation_and_spares_platform_tenant(client, as_platform):
    assert client.post("/api/admin/platform/tenants/default/purge", params={"confirm": "admin"},
                       headers=as_platform).status_code == 409
    assert client.post(f"/api/admin/platform/tenants/{TB}/purge", params={"confirm": "wrong"},
                       headers=as_platform).status_code == 400
