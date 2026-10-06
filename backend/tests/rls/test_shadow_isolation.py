"""AI-05 (alembic 056): shadow (experimental) results never become clinical results.

Same harness as test_tenant_isolation.py (skipped unless RLS_TEST_OWNER_DATABASE_URL).
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_B,
    TB,
    as_b,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

USECASE = "brain_mri"


def _result(**over) -> dict:
    data = {"id": str(uuid.uuid4()), "study_instance_uid": STUDY_B, "usecase_name": USECASE,
            "job_id": None, "summary": {"which": "clinical"}, "measurements": {}, "qa_flags": [],
            "qa_details": {}, "model_version": "1.0", "model_checksum": "c", "artifacts": []}
    data.update(over)
    return data


@pytest.fixture(scope="module")
def clinical_and_shadow(seeded):
    from sqlalchemy.orm import Session

    from app.infrastructure.queue.tasks import _save_result

    engine = sa.create_engine(OWNER_URL)
    with Session(engine) as s:
        s.execute(sa.text("SET app.platform = 'on'"))
        clinical = _result()
        _save_result(s, clinical, tenant_id=TB)
        s.execute(sa.text("SET app.platform = 'on'"))
        shadow = _result(summary={"which": "shadow"}, model_version="2.0-candidate")
        _save_result(s, shadow, tenant_id=TB, shadow=True)
    yield clinical["id"], shadow["id"]
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM results_index WHERE id IN (:a, :b)"),
                  {"a": clinical["id"], "b": shadow["id"]})
    engine.dispose()


def _row(result_id: str):
    engine = sa.create_engine(OWNER_URL)
    with engine.begin() as c:
        row = c.execute(sa.text(
            "SELECT is_latest, is_shadow, version FROM results_index WHERE id = :i"), {"i": result_id}).first()
    engine.dispose()
    return row


def test_shadow_result_never_demotes_the_clinical_one(clinical_and_shadow):
    clinical_id, shadow_id = clinical_and_shadow
    assert tuple(_row(clinical_id)) == (True, False, _row(clinical_id)[2])
    assert _row(shadow_id)[:2] == (False, True)


def test_database_refuses_a_latest_shadow_result(seeded):
    engine = sa.create_engine(OWNER_URL)
    with pytest.raises(sa.exc.IntegrityError, match="ck_results_shadow_not_latest"):
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO results_index (id, study_instance_uid, usecase_name, summary, measurements, "
                "qa_flags, qa_details, model_version, model_checksum, artifacts, version, is_latest, "
                "is_shadow, tenant_id, created_at) VALUES (:id, :s, 'brain_mri', '{}', '{}', '[]', '{}', "
                "'x', 'x', '[]', 0, true, true, :t, now())"), {"id": str(uuid.uuid4()), "s": STUDY_B, "t": TB})
    engine.dispose()


def test_clinical_api_never_shows_shadow_results(client, as_b, clinical_and_shadow):
    clinical_id, shadow_id = clinical_and_shadow
    latest = client.get(f"/api/results/{STUDY_B}/{USECASE}", headers=as_b)
    assert latest.status_code == 200, latest.text
    assert latest.json()["id"] == clinical_id
    versions = client.get(f"/api/results/{STUDY_B}/{USECASE}/versions", headers=as_b)
    assert versions.status_code == 200, versions.text
    assert shadow_id not in versions.text
    listing = client.get(f"/api/results/{STUDY_B}", headers=as_b)
    assert shadow_id not in listing.text
