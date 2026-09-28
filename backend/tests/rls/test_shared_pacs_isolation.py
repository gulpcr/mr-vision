"""Phase 2 isolation: viewer sessions, DICOMweb authorization, AE-title attribution,
cross-tenant study ownership, and tenant-addressed WebSockets.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL is
set); reuses its seeded tenants/studies.
"""
from __future__ import annotations

import pytest

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_A,
    STUDY_B,
    TA,
    TB,
    _auth,
    _run_isolated,
    as_a,
    as_b,
    as_platform,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")


def _viewer_cookie(user_id: str, tenant_id: str, platform: bool = False) -> dict:
    from app.application.auth_service import AuthService
    from app.config import get_settings

    token = AuthService(session=None).create_viewer_token(
        subject=user_id, tenant_id=tenant_id, is_platform_admin=platform,
    )
    return {get_settings().viewer_cookie_name: token}


def _authz(client, cookies: dict | None, uri: str, method: str = "GET") -> int:
    client.cookies.clear()
    for k, v in (cookies or {}).items():
        client.cookies.set(k, v)
    r = client.get(
        "/api/internal/dicomweb-authz",
        headers={"X-Original-URI": uri, "X-Original-Method": method},
    )
    client.cookies.clear()
    return r.status_code


def test_dicomweb_authz_confines_viewer_to_own_tenants_studies(client, seeded):
    b = _viewer_cookie(seeded["rad_b"], TB)
    assert _authz(client, b, f"/dicom-web/studies/{STUDY_B}/series/1.2/instances/3/frames/1") == 204
    assert _authz(client, b, f"/dicom-web/studies/{STUDY_A}/series/1.2/instances/3/frames/1") == 403
    assert _authz(client, b, f"/dicom-web/studies/{STUDY_A}/metadata") == 403
    assert _authz(client, b, f"/wado?requestType=WADO&studyUID={STUDY_A}&objectUID=9") == 403
    assert _authz(client, b, f"/wado?requestType=WADO&studyUID={STUDY_B}&objectUID=9") == 204


def test_dicomweb_authz_rejects_missing_session_writes_and_traversal(client, seeded):
    b = _viewer_cookie(seeded["rad_b"], TB)
    assert _authz(client, None, f"/dicom-web/studies/{STUDY_B}/metadata") == 401
    assert _authz(client, b, f"/dicom-web/studies/{STUDY_B}", method="DELETE") == 403
    assert _authz(client, b, f"/dicom-web/studies/{STUDY_B}/../{STUDY_A}/metadata") == 403
    assert _authz(client, b, f"/dicom-web/studies/{STUDY_B}/%2e%2e/{STUDY_A}") == 403
    # Not scoped to a study → platform admins only.
    assert _authz(client, b, "/dicom-web/instances?PatientID=MRN-SHARED") == 403
    # Study search is allowed through (served by the tenant-filtered QIDO proxy).
    assert _authz(client, b, "/dicom-web/studies?PatientID=MRN-SHARED") == 204


def test_platform_admin_viewer_can_reach_any_study(client, seeded):
    p = _viewer_cookie(seeded["platform"], "default", platform=True)
    assert _authz(client, p, f"/dicom-web/studies/{STUDY_A}/metadata") == 204
    assert _authz(client, p, "/dicom-web/instances?PatientID=x") == 204


def test_viewer_token_is_not_an_api_credential(client, seeded):
    token = next(iter(_viewer_cookie(seeded["admin_b"], TB).values()))
    r = client.get("/api/studies", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_login_sets_httponly_viewer_cookie(client, seeded):
    import sqlalchemy as sa

    from app.application.auth_service import AuthService
    from app.config import get_settings

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text("UPDATE users SET hashed_password = :h WHERE id = :id"),
                  {"h": AuthService._hash_password("Viewer-Pass-7"), "id": seeded["rad_a"]})
    engine.dispose()
    r = client.post("/api/auth/login", json={"username": "rls_rad_a", "password": "Viewer-Pass-7"})
    assert r.status_code == 200, r.text
    set_cookie = r.headers.get("set-cookie", "")
    assert get_settings().viewer_cookie_name in set_cookie
    assert "httponly" in set_cookie.lower()
    client.cookies.clear()


def test_study_owner_function_sees_past_rls(seeded):
    """study_owner_tenant() lets a tenant-scoped upload refuse another tenant's UID."""
    from app.infrastructure.database.repositories import PgStudyRepository
    from app.infrastructure.database.session import async_session_factory
    from app.infrastructure.tenant.db_scope import tenant_scope

    async def _owner():
        with tenant_scope(TB):
            async with async_session_factory() as s:
                repo = PgStudyRepository(s, tenant_id=TB)
                visible = await repo.get_by_uid(STUDY_A)
                return visible, await repo.owner_tenant_of(STUDY_A), await repo.owner_tenant_of("9.9.9")

    visible, owner, nobody = _run_isolated(_owner)
    assert visible is None          # RLS hides tenant A's study from tenant B …
    assert owner == TA              # … but ownership is still detectable
    assert nobody is None


def test_ae_title_assignment_and_resolution(client, as_platform, seeded):
    r = client.post(f"/api/admin/tenants/{TA}/dicom-endpoints",
                    json={"called_aet": "rls_hospa"}, headers=as_platform)
    assert r.status_code == 201, r.text
    assert r.json()["called_aet"] == "RLS_HOSPA"
    # An AE title belongs to exactly one tenant.
    r = client.post(f"/api/admin/tenants/{TB}/dicom-endpoints",
                    json={"called_aet": "RLS_HOSPA"}, headers=as_platform)
    assert r.status_code == 409
    r = client.post(f"/api/admin/tenants/{TB}/dicom-endpoints",
                    json={"called_aet": "RLS_HOSPB", "calling_aet": "CT_SCANNER_1"},
                    headers=as_platform)
    assert r.status_code == 201, r.text

    from app.application.dicom_endpoint_service import DicomEndpointService
    from app.infrastructure.database.session import async_session_factory
    from app.infrastructure.tenant.db_scope import platform_scope

    async def _resolve():
        with platform_scope():
            async with async_session_factory() as s:
                svc = DicomEndpointService(s)
                return (
                    await svc.resolve_tenant("rls_hospa", "ANY_SCANNER"),
                    await svc.resolve_tenant("RLS_HOSPB", "CT_SCANNER_1"),
                    await svc.resolve_tenant("RLS_HOSPB", "OTHER_SCANNER"),
                    await svc.resolve_tenant("UNKNOWN_AE", None),
                )

    assert _run_isolated(_resolve) == (TA, TB, None, None)

    # Tenant admins cannot manage AE titles (platform-only surface).
    assert client.get(f"/api/admin/tenants/{TA}/dicom-endpoints", headers=as_a_admin(seeded)).status_code == 403


def as_a_admin(seeded) -> dict:
    return _auth(seeded["admin_a"], "rls_admin_a", "admin", TA)


def test_websocket_requires_viewer_session_and_is_tenant_addressed(client, seeded):
    from starlette.websockets import WebSocketDisconnect

    from app.interface.api.ws import manager

    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()

    for k, v in _viewer_cookie(seeded["rad_a"], TA).items():
        client.cookies.set(k, v)
    with client.websocket_connect("/ws") as ws_a:
        client.cookies.clear()
        for k, v in _viewer_cookie(seeded["rad_b"], TB).items():
            client.cookies.set(k, v)
        with client.websocket_connect("/ws") as ws_b:
            client.cookies.clear()
            ws_a.send_json({"type": "ping"})
            assert ws_a.receive_json() == {"type": "pong"}  # both sockets registered
            ws_b.send_json({"type": "ping"})
            assert ws_b.receive_json() == {"type": "pong"}

            client.portal.call(manager.send_to_tenant, TB, {"type": "job_update", "job_id": "jb"})
            assert ws_b.receive_json()["job_id"] == "jb"
            client.portal.call(manager.send_to_tenant, TA, {"type": "job_update", "job_id": "ja"})
            # A's socket gets only A's event: the next message on it is "ja", never "jb".
            assert ws_a.receive_json()["job_id"] == "ja"
