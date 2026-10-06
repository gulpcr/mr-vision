"""Break-glass emergency access (HIPAA 164.312(a)(2)(ii)).

A referring Doctor normally sees only referred patients. With a stated reason they get
time-limited access to one patient; it is audited, visible to admins, and revocable.
Same harness as test_tenant_isolation.py.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from tests.rls.test_login_hardening import _bearer, _login, _make_user
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_B,
    TB,
    as_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

MRN = "MRN-SHARED"  # STUDY_B's patient in tenant B (not referred by anyone)
REASON = "Patient collapsed in ED, prior imaging needed to rule out bleed"


@pytest.fixture
def doctor(client, seeded):
    username = _make_user(role="doctor")
    return {"username": username, "headers": _bearer(_login(client, username))}


def _can_open_study(client, headers) -> bool:
    return client.get(f"/api/studies/{STUDY_B}", headers=headers).status_code == 200


def test_doctor_cannot_see_unreferred_patient_without_break_glass(client, doctor):
    assert not _can_open_study(client, doctor["headers"])


def test_reason_and_known_mrn_are_required(client, doctor):
    short = client.post("/api/break-glass", headers=doctor["headers"],
                        json={"patient_id": MRN, "reason": "urgent"})
    assert short.status_code == 400
    unknown = client.post("/api/break-glass", headers=doctor["headers"],
                          json={"patient_id": "NO-SUCH-MRN", "reason": REASON})
    assert unknown.status_code == 400


def test_grant_opens_the_patient_is_audited_and_revocable(client, doctor, as_b):
    r = client.post("/api/break-glass", headers=doctor["headers"],
                    json={"patient_id": MRN, "reason": REASON})
    assert r.status_code == 201, r.text
    grant = r.json()
    assert grant["active"] and grant["patient_id"] == MRN

    assert _can_open_study(client, doctor["headers"])
    listed = {s["study_instance_uid"] for s in
              client.get("/api/studies", headers=doctor["headers"]).json()["studies"]}
    assert STUDY_B in listed

    # The viewer (DICOMweb) follows the same rule.
    login = _login(client, doctor["username"])
    client.cookies.clear()
    client.cookies.set("mrv_viewer", login.cookies.get("mrv_viewer"), path="/")
    authz = client.get("/api/internal/dicomweb-authz", headers={
        "X-Original-URI": f"/dicom-web/studies/{STUDY_B}/series", "X-Original-Method": "GET"})
    assert authz.status_code == 204

    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        audited = c.execute(sa.text(
            "SELECT count(*) FROM audit_log WHERE action = 'break_glass_invoked' "
            "AND details->>'grant_id' = :g"), {"g": grant["id"]}).scalar()
    engine.dispose()
    assert audited == 1

    review = client.get("/api/break-glass", headers=as_b).json()["grants"]
    assert any(g["id"] == grant["id"] and g["reason"] == REASON for g in review)

    assert client.post(f"/api/break-glass/{grant['id']}/revoke", headers=as_b).status_code == 200
    assert not _can_open_study(client, doctor["headers"])


def test_expired_grant_no_longer_opens_the_patient(client, doctor):
    r = client.post("/api/break-glass", headers=doctor["headers"],
                    json={"patient_id": MRN, "reason": REASON})
    assert r.status_code == 201
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text("UPDATE break_glass_grants SET expires_at = now() - interval '1 minute' "
                          "WHERE id = :g"), {"g": r.json()["id"]})
    engine.dispose()
    assert not _can_open_study(client, doctor["headers"])


def test_roles_without_the_permission_cannot_break_glass(client, seeded):
    viewer = _bearer(_login(client, _make_user(role="viewer")))
    r = client.post("/api/break-glass", headers=viewer, json={"patient_id": MRN, "reason": REASON})
    assert r.status_code == 403
