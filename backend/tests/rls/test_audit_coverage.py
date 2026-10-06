"""TEC-07: every state-changing event the matrix lists writes a hash-chained audit entry
(auth, role change, DICOM ingest, inference trigger, draft edit, sign-off), changes carry
previous/new state hashes, and the chain stays valid.

Same harness as test_tenant_isolation.py; the ingest part needs the TLS test stack's
Orthanc (RLS_TEST_WITH_ORTHANC=1, as test_viewer_name_mask.py).
"""
from __future__ import annotations

import io
import os
import uuid

import pytest
import sqlalchemy as sa

from tests.rls.test_login_hardening import _login, _make_user
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    TB,
    as_b,
    as_platform,
    as_rad_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(
    not (OWNER_URL and os.environ.get("RLS_TEST_WITH_ORTHANC")),
    reason="needs RLS_TEST_OWNER_DATABASE_URL and RLS_TEST_WITH_ORTHANC",
)


def _entries(action: str, entity_id: str | None = None):
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        sql = "SELECT seq, row_hash, details FROM audit_log WHERE action = :a"
        params = {"a": action}
        if entity_id is not None:
            sql += " AND entity_id = :e"
            params["e"] = entity_id
        rows = c.execute(sa.text(sql + " ORDER BY timestamp"), params).fetchall()
    engine.dispose()
    return rows


def _chained(rows) -> bool:
    return bool(rows) and all(r[0] is not None and r[1] for r in rows)


def _upload_instance(study_uid: str) -> None:
    import httpx
    import numpy as np
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

    from app.config import get_settings
    from app.infrastructure.tls import httpx_verify

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = MRImageStorage, meta.MediaStorageSOPInstanceUID
    ds.PatientName, ds.PatientID = "Audit^Coverage", "MRN-AUDIT"
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.Modality = study_uid, generate_uid(), "MR"
    ds.BodyPartExamined, ds.StudyDescription = "BRAIN", "MRI BRAIN"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = np.zeros(16, dtype=np.uint16).tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    s = get_settings()
    httpx.post(f"{s.orthanc_url}/instances", content=buf.getvalue(),
               auth=(s.orthanc_username, s.orthanc_password),
               verify=httpx_verify(s.orthanc_ca_cert), timeout=30).raise_for_status()


def test_every_listed_event_is_audited_in_the_chain(client, seeded, as_b, as_rad_b, as_platform):
    # 1. authentication
    username = _make_user()
    assert _login(client, username).status_code == 200
    assert _chained(_entries("user_login"))

    # 2. role change (user) — previous / new state hashes
    user_id = _entries("user_login")[-1][2].get("user_id") or None
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        user_id = c.execute(sa.text("SELECT id FROM users WHERE username = :u"), {"u": username}).scalar()
    engine.dispose()
    r = client.put(f"/api/auth/users/{user_id}/role?role=technician", headers=as_b)
    assert r.status_code == 200, r.text
    rows = _entries("user_role_changed", user_id)
    assert _chained(rows)
    assert rows[-1][2]["previous_role"] == "viewer" and len(rows[-1][2]["state_after_sha256"]) == 64

    # 2b. role definition change — via the (now chained) role service
    created = client.post("/api/roles", headers=as_b,
                          json={"name": f"audit-{uuid.uuid4().hex[:6]}", "permissions": ["study.view"]})
    assert created.status_code == 201, created.text
    role_id = created.json()["id"]
    assert client.patch(f"/api/roles/{role_id}", headers=as_b,
                        json={"permissions": ["study.view", "result.export"]}).status_code == 200
    rows = _entries("role_updated", role_id)
    assert _chained(rows) and rows[-1][2]["state_before_sha256"] != rows[-1][2]["state_after_sha256"]

    # 3. DICOM ingest  4. inference trigger
    study_uid = f"1.2.826.0.1.9077.{uuid.uuid4().int % 10**12}"
    _upload_instance(study_uid)
    r = client.post("/api/studies", headers=as_b, json={"study_instance_uid": study_uid})
    assert r.status_code == 201, r.text
    assert _chained(_entries("study_received", study_uid))
    r = client.post(f"/api/studies/{study_uid}/jobs", headers=as_b, json={"usecase_names": ["brain_mri"]})
    assert r.status_code == 201, r.text
    job_id = r.json()["jobs"][0]["id"]
    assert _chained(_entries("job_created", job_id))

    # 5. draft edit (radiologist-authored report) — state hashes, not the PHI text
    r = client.put(f"/api/studies/{study_uid}/mammography-report", headers=as_rad_b,
                   json={"opinion": "Benign-appearing findings.", "birads_right": "2"})
    assert r.status_code == 200, r.text
    rows = _entries("mammography_report_saved", study_uid)
    assert _chained(rows) and "state_after_sha256" in rows[-1][2]
    assert "Benign-appearing" not in str(rows[-1][2])

    # 6. sign-off
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO results_index (id, study_instance_uid, usecase_name, summary, measurements, "
            "qa_flags, qa_details, model_version, model_checksum, artifacts, version, is_latest, "
            "tenant_id, created_at) VALUES (:id, :s, 'brain_mri', '{}', '{}', '[]', '{}', '1', 'c', '[]', "
            "1, true, :t, now())"), {"id": str(uuid.uuid4()), "s": study_uid, "t": TB})
    engine.dispose()
    r = client.post(f"/api/studies/{study_uid}/signature", headers=as_rad_b,
                    json={"statement_version": "comprehensive-v1", "agreed": True, "comment": "Reviewed."})
    assert r.status_code == 200, r.text
    assert _chained(_entries("report_esigned", study_uid))

    # The whole chain still verifies.
    verify = client.get("/api/admin/audit/verify", headers=as_platform).json()
    assert verify["valid"] is True, verify
