"""Patient rights (HIPAA 164.524 access, 164.528 accounting) and retention disposal
(164.310(d)(2)). Same harness as test_tenant_isolation.py."""
from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile

import pytest
import sqlalchemy as sa

from tests.rls.test_login_hardening import _bearer, _login, _make_user
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    TB,
    as_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

MRN = "MRN-SHARED"


# ── right of access / accounting ────────────────────────────────────────────

def test_access_export_zip_is_complete_and_recorded_as_a_disclosure(client, seeded, as_b):
    r = client.post(f"/api/patients/{MRN}/access-export", headers=as_b,
                    json={"requested_by": "Patient in person, ID checked"})
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["patient"]["mrn"] == MRN and manifest["contents"]["studies"] >= 1

    report = client.get(f"/api/patients/{MRN}/access-report", headers=as_b).json()
    assert any(d["action"] == "patient_record_disclosed" for d in report["disclosures"])


def test_access_export_can_be_aes_encrypted(client, seeded, as_b):
    pyzipper = pytest.importorskip("pyzipper")
    passphrase = "correct horse battery staple"
    r = client.post(f"/api/patients/{MRN}/access-export", headers=as_b,
                    json={"requested_by": "Patient in person, ID checked", "passphrase": passphrase})
    assert r.status_code == 200, r.text
    # Plain zipfile cannot read it; with the passphrase it opens and is complete.
    with pytest.raises(Exception):
        zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json")
    with pyzipper.AESZipFile(io.BytesIO(r.content)) as zf:
        zf.setpassword(passphrase.encode())
        assert json.loads(zf.read("manifest.json"))["patient"]["mrn"] == MRN
    assert passphrase.encode() not in r.content
    short = client.post(f"/api/patients/{MRN}/access-export", headers=as_b,
                        json={"requested_by": "Patient in person", "passphrase": "short"})
    assert short.status_code == 422


def test_access_report_csv_and_unknown_patient(client, seeded, as_b):
    csv = client.get(f"/api/patients/{MRN}/access-report?format=csv", headers=as_b)
    assert csv.status_code == 200 and csv.text.startswith("kind,timestamp,action")
    assert client.get("/api/patients/NO-SUCH/access-report", headers=as_b).status_code == 404


def test_patient_rights_need_the_permission(client, seeded):
    viewer = _bearer(_login(client, _make_user(role="viewer")))
    assert client.get(f"/api/patients/{MRN}/access-report", headers=viewer).status_code == 403
    assert client.post(f"/api/patients/{MRN}/access-export", headers=viewer,
                       json={"requested_by": "someone"}).status_code == 403


# ── retention disposal ──────────────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.deleted: list[str] = []

    async def delete(self, path):
        self.deleted.append(path)


class FakePacs:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted: list[str] = []

    async def delete_study_by_uid(self, uid):
        if self.fail:
            raise RuntimeError("pacs down")
        self.deleted.append(uid)
        return True


def _seed_old_study() -> tuple[str, str]:
    uid = f"1.2.826.0.1.7777.{uuid.uuid4().int % 10**9}"
    policy_id = str(uuid.uuid4())
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO studies (study_instance_uid, tenant_id, patient_id, modality, reading_status, "
            "created_at, updated_at) VALUES (:s, :t, 'MRN-OLD', 'CT', 'reported', "
            "now() - interval '3000 days', now())"), {"s": uid, "t": TB})
        c.execute(sa.text(
            "INSERT INTO results_index (id, study_instance_uid, usecase_name, job_id, summary, "
            "measurements, qa_flags, qa_details, model_version, model_checksum, artifacts, "
            "created_at, version, is_latest, tenant_id) VALUES (:id, :s, 'abdomen_ct', NULL, '{}', '{}', "
            "'[]', '{}', 'v', 'c', :a, now() - interval '3000 days', 1, true, :t)"),
            {"id": str(uuid.uuid4()), "s": uid, "t": TB,
             "a": json.dumps([{"name": "tile.png", "artifact_type": "x", "storage_path": f"{TB}/{uid}/tile.png"}])})
        c.execute(sa.text(
            "INSERT INTO retention_policies (id, name, entity_type, max_age_days, action, is_active, "
            "tenant_id, created_at) VALUES (:id, :n, 'study', 365, 'delete', true, :t, now())"),
            {"id": policy_id, "n": f"purge-{policy_id[:6]}", "t": TB})
    engine.dispose()
    return uid, policy_id


def _study_exists(uid: str) -> bool:
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        n = c.execute(sa.text("SELECT count(*) FROM studies WHERE study_instance_uid = :s"), {"s": uid}).scalar()
    engine.dispose()
    return bool(n)


def _drop_policy(policy_id: str) -> None:
    """Remove the test's policy and any seeded studies a test deliberately left behind."""
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM retention_policies WHERE id = :p"), {"p": policy_id})
        c.execute(sa.text("DELETE FROM results_index WHERE study_instance_uid IN "
                          "(SELECT study_instance_uid FROM studies WHERE patient_id = 'MRN-OLD')"))
        c.execute(sa.text("DELETE FROM studies WHERE patient_id = 'MRN-OLD'"))
    engine.dispose()


def _apply(store, pacs):
    from app.application.retention_service import RetentionService
    from app.infrastructure.database.session import async_session_factory, engine
    from app.infrastructure.tenant.db_scope import platform_scope

    async def run():
        with platform_scope():
            async with async_session_factory() as s:
                totals = await RetentionService(s, store, pacs).apply_policies(tenant_id=TB)
                await s.commit()
        await engine.dispose()
        return totals

    return asyncio.new_event_loop().run_until_complete(run())


@pytest.fixture
def retention_on():
    from app.config import get_settings

    s = get_settings()
    saved = (s.retention_enabled, s.retention_min_days)
    s.retention_enabled, s.retention_min_days = True, 0
    yield s
    s.retention_enabled, s.retention_min_days = saved


def test_retention_is_a_no_op_when_disabled(seeded):
    uid, policy = _seed_old_study()
    try:
        assert _apply(FakeStore(), FakePacs()) == {}
        assert _study_exists(uid)
    finally:
        _drop_policy(policy)


def test_purge_removes_images_and_artifacts_with_the_rows(seeded, retention_on):
    uid, policy = _seed_old_study()
    store, pacs = FakeStore(), FakePacs()
    try:
        _apply(store, pacs)
        assert not _study_exists(uid)
        assert uid in pacs.deleted
        assert f"{TB}/{uid}/tile.png" in store.deleted
    finally:
        _drop_policy(policy)


def test_failed_image_deletion_keeps_the_study_for_retry(seeded, retention_on):
    uid, policy = _seed_old_study()
    try:
        _apply(FakeStore(), FakePacs(fail=True))
        assert _study_exists(uid)
    finally:
        _drop_policy(policy)


def test_retention_floor_protects_recent_records(seeded, retention_on):
    retention_on.retention_min_days = 4000  # older than the 3000-day seeded study
    uid, policy = _seed_old_study()
    try:
        _apply(FakeStore(), FakePacs())
        assert _study_exists(uid)
    finally:
        _drop_policy(policy)
