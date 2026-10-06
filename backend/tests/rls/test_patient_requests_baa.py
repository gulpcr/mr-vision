"""PRV-04 patient-request register + 164.522 restrictions, ADM-01 tenant BAA gate
(alembic 057). Same harness as test_review_signoff.py."""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.rls.test_review_signoff import BASE, _add_result, _owner_exec, _sign, world  # noqa: F401
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    TB,
    as_a,
    as_b,
    as_platform,
    as_rad_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

MRN = f"MRN-R{uuid.uuid4().hex[:6]}"


# ── request register ─────────────────────────────────────────────────────────

def _log(client, headers, **kw):
    body = {"mrn": MRN, "request_type": "access", "requester": "Patient in person, ID checked"}
    body.update(kw)
    return client.post("/api/patient-rights/requests", headers=headers, json=body)


def test_request_clock_extension_and_closure(client, seeded, as_b, as_a):
    r = _log(client, as_b)
    assert r.status_code == 201, r.text
    req = r.json()
    received = datetime.fromisoformat(req["received_at"])
    assert datetime.fromisoformat(req["due_at"]) - received == timedelta(days=30)
    assert req["status"] == "open" and not req["overdue"]

    # One documented extension: +30 days, reason required, only once.
    assert client.post(f"/api/patient-rights/requests/{req['id']}/extend", headers=as_b,
                       json={"reason": ""}).status_code == 422
    ext = client.post(f"/api/patient-rights/requests/{req['id']}/extend", headers=as_b,
                      json={"reason": "Records archived off-site; copy ready by the new date"})
    assert ext.status_code == 200, ext.text
    assert datetime.fromisoformat(ext.json()["due_at"]) - received == timedelta(days=60)
    assert client.post(f"/api/patient-rights/requests/{req['id']}/extend", headers=as_b,
                       json={"reason": "again"}).status_code == 422

    # A denial needs the reason; a closed request stays closed.
    assert client.post(f"/api/patient-rights/requests/{req['id']}/close", headers=as_b,
                       json={"outcome": "denied", "notes": ""}).status_code == 422
    done = client.post(f"/api/patient-rights/requests/{req['id']}/close", headers=as_b,
                       json={"outcome": "fulfilled", "notes": "Copy handed over"})
    assert done.status_code == 200 and done.json()["status"] == "fulfilled"
    assert client.post(f"/api/patient-rights/requests/{req['id']}/close", headers=as_b,
                       json={"outcome": "denied", "notes": "x"}).status_code == 422

    # Other tenants never see it.
    assert all(x["id"] != req["id"] for x in
               client.get("/api/patient-rights/requests", headers=as_a).json())
    audit = _owner_exec("SELECT action, details FROM audit_log WHERE entity_id = :e ORDER BY seq",
                        {"e": req["id"]})
    assert [a for a, _ in audit] == ["patient_request_logged", "patient_request_extended",
                                     "patient_request_closed"]
    assert all("state_after_sha256" in (d or {}) for _, d in audit)


def test_overdue_requests_are_flagged(client, seeded, as_b):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    r = _log(client, as_b, received_at=old, request_type="access")
    assert r.status_code == 201 and r.json()["overdue"]
    overdue = client.get("/api/patient-rights/requests", headers=as_b, params={"status": "overdue"}).json()
    assert r.json()["id"] in {x["id"] for x in overdue}
    # Too late to extend: the extension has to be given within the first 30 days.
    assert client.post(f"/api/patient-rights/requests/{r.json()['id']}/extend", headers=as_b,
                       json={"reason": "late"}).status_code == 422
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert _log(client, as_b, received_at=future).status_code == 422
    # Amendment requests run on 60 days (164.526).
    am = _log(client, as_b, request_type="amendment").json()
    assert (datetime.fromisoformat(am["due_at"]) - datetime.fromisoformat(am["received_at"])).days == 60


def test_register_needs_the_permission(client, seeded):
    from tests.rls.test_login_hardening import _bearer, _login, _make_user

    viewer = _bearer(_login(client, _make_user(role="viewer")))
    assert client.get("/api/patient-rights/requests", headers=viewer).status_code == 403
    assert _log(client, viewer).status_code == 403


# ── restrictions ─────────────────────────────────────────────────────────────

def test_restrictions_block_each_outbound_channel(client, world, as_b, as_rad_b, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "dicom_sr_enabled", True)
    monkeypatch.setattr(s, "fhir_enabled", True)
    study = f"{BASE}.81"
    _owner_exec(
        "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, patient_name, modality, "
        "reading_status, created_at, updated_at) VALUES (:s, :t, :m, 'Pt 81', 'MR', 'unread', "
        "now(), now())", {"s": study, "t": TB, "m": MRN},
    )
    _add_result(study, "brain_mri", {"tumor_detected": False})
    result_id = _owner_exec("SELECT id FROM results_index WHERE study_instance_uid = :s", {"s": study})[0][0]
    assert _sign(client, as_rad_b, study, comment="Normal study.").status_code == 200
    sr = f"/api/reports/{study}/brain_mri/dicom-sr"
    assert client.get(sr, headers=as_rad_b).status_code == 200

    r = client.post("/api/patient-rights/restrictions", headers=as_b, json={
        "mrn": MRN, "channel": "dicom_export", "reason": "Patient asked: no copies to the PACS"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    blocked = client.get(sr, headers=as_rad_b)
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["code"] == "patient_restriction" and "164.522" in detail["message"]
    assert "Patient Rights" in detail["remedy"]
    assert detail["action"]["path"] == "/admin/patient-rights"
    assert client.post(f"/api/results/{result_id}/export-dicom", headers=as_rad_b).status_code == 409
    # Other channels are unaffected by a dicom_export restriction.
    assert client.post(f"/api/results/{result_id}/share", headers=as_b, json={}).status_code == 200

    every = client.post("/api/patient-rights/restrictions", headers=as_b, json={
        "mrn": MRN, "channel": "all", "reason": "Patient asked: nothing leaves"}).json()
    assert client.get(f"/api/reports/{study}/brain_mri/fhir", headers=as_rad_b).status_code == 409
    assert client.post(f"/api/results/{result_id}/share", headers=as_b, json={}).status_code == 409
    assert _webhooks_restricted(study)

    for x in (rid, every["id"]):
        assert client.post(f"/api/patient-rights/restrictions/{x}/revoke", headers=as_b).json()["active"] is False
    assert client.get(sr, headers=as_rad_b).status_code == 200
    assert not _webhooks_restricted(study)
    refused = _owner_exec("SELECT count(*) FROM audit_log WHERE action = 'disclosure_blocked_by_restriction' "
                          "AND entity_id = :s", {"s": study})[0][0]
    assert refused >= 3


def _webhooks_restricted(study: str) -> bool:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.application.patient_rights_service import PatientRightsService
    from app.infrastructure.tls import db_async_connect_args

    async def go():
        url = OWNER_URL.split("?", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
        eng = create_async_engine(url, connect_args=db_async_connect_args())
        try:
            async with AsyncSession(eng) as s:
                return await PatientRightsService(s, TB).study_restricted(study, "webhooks")
        finally:
            await eng.dispose()

    return asyncio.new_event_loop().run_until_complete(go())


def test_restriction_validation(client, seeded, as_b):
    assert client.post("/api/patient-rights/restrictions", headers=as_b, json={
        "mrn": MRN, "channel": "email", "reason": "x" * 5}).status_code == 422
    expired = client.post("/api/patient-rights/restrictions", headers=as_b, json={
        "mrn": MRN, "channel": "fhir", "reason": "Until a date",
        "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}).json()
    assert expired["active"] is False


# ── tenant BAA gate ──────────────────────────────────────────────────────────

SLUG = f"baa-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def baa_required(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "require_tenant_baa", True)


def test_tenant_needs_a_baa_before_phi_can_flow(client, seeded, as_platform, baa_required):
    base = {"name": f"Hospital {SLUG}", "slug": SLUG, "plan": "starter", "features": [],
            "admin_username": "baaadmin", "admin_email": f"{SLUG}@example.com"}
    r = client.post("/api/admin/tenants", headers=as_platform, json={**base, "called_aet": "BAA1"})
    assert r.status_code == 400 and "BAA" in r.json()["detail"]
    r = client.post("/api/admin/tenants", headers=as_platform, json=base)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    try:
        assert r.json()["status"] == "suspended" and r.json()["is_active"] is False
        t = f"/api/admin/tenants/{tid}"
        assert client.put(f"{t}/status", headers=as_platform, json={"status": "active"}).status_code == 409
        assert client.post(f"{t}/dicom-endpoints", headers=as_platform,
                           json={"called_aet": "BAA1"}).status_code == 409
        assert client.post(f"{t}/api-keys", headers=as_platform,
                           json={"name": "modality", "scopes": ["dicom:upload"]}).status_code == 409

        doc = hashlib.sha256(b"signed BAA pdf").hexdigest()
        baa = {"counterparty": "Hospital Ltd", "signatory_name": "Jane CEO", "signatory_title": "CEO",
               "signed_on": date.today().isoformat(), "effective_from": date.today().isoformat(),
               "document_name": "BAA-2026.pdf", "document_sha256": "nothex" * 10 + "abcd"}
        assert client.post(f"{t}/baas", headers=as_platform, json=baa).status_code == 422
        rec = client.post(f"{t}/baas", headers=as_platform, json={**baa, "document_sha256": doc})
        assert rec.status_code == 201, rec.text
        assert rec.json()["active"] is True

        assert client.put(f"{t}/status", headers=as_platform, json={"status": "active"}).status_code == 200
        assert client.post(f"{t}/dicom-endpoints", headers=as_platform,
                           json={"called_aet": f"B{SLUG[-8:]}".upper()[:16]}).status_code == 201
        assert client.post(f"{t}/api-keys", headers=as_platform,
                           json={"name": "modality", "scopes": ["dicom:upload"]}).status_code == 201

        # Terminated: no new keys or endpoints (the workspace itself is offboarded by contract).
        client.post(f"{t}/baas/{rec.json()['id']}/terminate", headers=as_platform)
        assert client.post(f"{t}/api-keys", headers=as_platform,
                           json={"name": "m2", "scopes": ["dicom:upload"]}).status_code == 409
        listed = client.get(f"{t}/baas", headers=as_platform).json()
        assert listed[0]["terminated_at"] and listed[0]["document_sha256"] == doc
    finally:
        purge = client.post(f"/api/admin/platform/tenants/{tid}/purge",
                            params={"confirm": SLUG}, headers=as_platform)
        assert purge.status_code == 200, purge.text


def test_baa_gate_off_by_default(client, seeded, as_platform):
    from app.config import get_settings

    assert get_settings().require_tenant_baa is False  # dev / test: unchanged behaviour
