"""Tenant row-level isolation — against a real schema (SQLite in-memory), not mocks.

Proves the actual SQL filtering PgStudyRepository/PgJobRepository/PgResultRepository/
PgSeriesRepository/PgAuditRepository apply when constructed with a tenant_id: a
tenant-scoped repository must never return, count, or let another tenant's row be
updated — and a write through it must land tagged with that tenant, not "default".
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.enums import JobStatus
from app.domain.models import AuditEntry, JobRun, Result, Series, Study, Tenant
from app.infrastructure.database.models import Base, StudyRecord
from app.infrastructure.database.repositories import (
    PgAuditRepository,
    PgJobRepository,
    PgResultRepository,
    PgSeriesRepository,
    PgStudyRepository,
    PgTenantRepository,
)


def _uid() -> str:
    return f"1.2.{uuid.uuid4().int % 10**12}"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_study(session, study_uid: str, tenant_id: str, patient_id: str | None = None) -> None:
    session.add(StudyRecord(study_instance_uid=study_uid, modality="MR", tenant_id=tenant_id, patient_id=patient_id))
    await session.flush()


class TestStudyRepositoryIsolation:
    @pytest.mark.asyncio
    async def test_list_studies_scoped_to_tenant(self, db_session):
        uid_a, uid_b = _uid(), _uid()
        await _seed_study(db_session, uid_a, "hospital-a")
        await _seed_study(db_session, uid_b, "hospital-b")
        await db_session.commit()

        repo_a = PgStudyRepository(db_session, tenant_id="hospital-a")
        studies_a = await repo_a.list_studies()
        assert {s.study_instance_uid for s in studies_a} == {uid_a}

        repo_b = PgStudyRepository(db_session, tenant_id="hospital-b")
        studies_b = await repo_b.list_studies()
        assert {s.study_instance_uid for s in studies_b} == {uid_b}

    @pytest.mark.asyncio
    async def test_get_by_uid_returns_none_for_other_tenant(self, db_session):
        uid_a = _uid()
        await _seed_study(db_session, uid_a, "hospital-a")
        await db_session.commit()

        repo_b = PgStudyRepository(db_session, tenant_id="hospital-b")
        assert await repo_b.get_by_uid(uid_a) is None

        repo_a = PgStudyRepository(db_session, tenant_id="hospital-a")
        fetched = await repo_a.get_by_uid(uid_a)
        assert fetched is not None and fetched.study_instance_uid == uid_a

    @pytest.mark.asyncio
    async def test_count_scoped_to_tenant(self, db_session):
        await _seed_study(db_session, _uid(), "hospital-a")
        await _seed_study(db_session, _uid(), "hospital-a")
        await _seed_study(db_session, _uid(), "hospital-b")
        await db_session.commit()

        assert await PgStudyRepository(db_session, tenant_id="hospital-a").count() == 2
        assert await PgStudyRepository(db_session, tenant_id="hospital-b").count() == 1

    @pytest.mark.asyncio
    async def test_update_does_not_affect_other_tenants_study(self, db_session):
        uid_a = _uid()
        await _seed_study(db_session, uid_a, "hospital-a")
        await db_session.commit()

        repo_b = PgStudyRepository(db_session, tenant_id="hospital-b")
        study = Study(study_instance_uid=uid_a, study_description="hijacked")
        await repo_b.update(study)
        await db_session.commit()

        repo_a = PgStudyRepository(db_session, tenant_id="hospital-a")
        untouched = await repo_a.get_by_uid(uid_a)
        assert untouched.study_description != "hijacked"

    @pytest.mark.asyncio
    async def test_save_stamps_ambient_tenant_id(self, db_session):
        uid = _uid()
        repo = PgStudyRepository(db_session, tenant_id="hospital-a")
        await repo.save(Study(study_instance_uid=uid, modality="MR"))
        await db_session.commit()

        fetched = await repo.get_by_uid(uid)
        assert fetched.tenant_id == "hospital-a"

    @pytest.mark.asyncio
    async def test_no_tenant_id_means_unscoped(self, db_session):
        """A repository built with tenant_id=None (system/internal use) sees everything."""
        await _seed_study(db_session, _uid(), "hospital-a")
        await _seed_study(db_session, _uid(), "hospital-b")
        await db_session.commit()

        repo = PgStudyRepository(db_session, tenant_id=None)
        assert await repo.count() == 2


class TestJobRepositoryIsolation:
    @pytest.mark.asyncio
    async def test_get_by_id_scoped_to_tenant(self, db_session):
        uid = _uid()
        await _seed_study(db_session, uid, "hospital-a")
        await db_session.commit()

        job = JobRun(id=str(uuid.uuid4()), study_instance_uid=uid, usecase_name="brain_mri", status=JobStatus.PENDING)
        await PgJobRepository(db_session, tenant_id="hospital-a").save(job)
        await db_session.commit()

        assert await PgJobRepository(db_session, tenant_id="hospital-b").get_by_id(job.id) is None
        found = await PgJobRepository(db_session, tenant_id="hospital-a").get_by_id(job.id)
        assert found is not None and found.id == job.id

    @pytest.mark.asyncio
    async def test_list_jobs_scoped_to_tenant(self, db_session):
        uid = _uid()
        await _seed_study(db_session, uid, "hospital-a")
        await db_session.commit()

        await PgJobRepository(db_session, tenant_id="hospital-a").save(
            JobRun(id=str(uuid.uuid4()), study_instance_uid=uid, usecase_name="brain_mri")
        )
        await db_session.commit()

        assert len(await PgJobRepository(db_session, tenant_id="hospital-a").list_jobs()) == 1
        assert len(await PgJobRepository(db_session, tenant_id="hospital-b").list_jobs()) == 0


class TestResultRepositoryIsolation:
    @pytest.mark.asyncio
    async def test_get_by_study_and_usecase_scoped_to_tenant(self, db_session):
        uid = _uid()
        await _seed_study(db_session, uid, "hospital-a")
        await db_session.commit()

        result = Result(study_instance_uid=uid, usecase_name="brain_mri", model_version="v1", model_checksum="c1")
        await PgResultRepository(db_session, tenant_id="hospital-a").save(result)
        await db_session.commit()

        other_tenant_repo = PgResultRepository(db_session, tenant_id="hospital-b")
        assert await other_tenant_repo.get_by_study_and_usecase(uid, "brain_mri") is None

        same_tenant_repo = PgResultRepository(db_session, tenant_id="hospital-a")
        found = await same_tenant_repo.get_by_study_and_usecase(uid, "brain_mri")
        assert found is not None and found.tenant_id == "hospital-a"

    @pytest.mark.asyncio
    async def test_list_by_study_scoped_to_tenant(self, db_session):
        uid = _uid()
        await _seed_study(db_session, uid, "hospital-a")
        await db_session.commit()

        await PgResultRepository(db_session, tenant_id="hospital-a").save(
            Result(study_instance_uid=uid, usecase_name="brain_mri", model_version="v1", model_checksum="c1")
        )
        await db_session.commit()

        assert len(await PgResultRepository(db_session, tenant_id="hospital-a").list_by_study(uid)) == 1
        assert len(await PgResultRepository(db_session, tenant_id="hospital-b").list_by_study(uid)) == 0


class TestSeriesRepositoryIsolation:
    @pytest.mark.asyncio
    async def test_list_by_study_scoped_to_tenant(self, db_session):
        uid = _uid()
        await _seed_study(db_session, uid, "hospital-a")
        await db_session.commit()

        series = Series(series_instance_uid=_uid(), study_instance_uid=uid)
        await PgSeriesRepository(db_session, tenant_id="hospital-a").save(series)
        await db_session.commit()

        assert len(await PgSeriesRepository(db_session, tenant_id="hospital-a").list_by_study(uid)) == 1
        assert len(await PgSeriesRepository(db_session, tenant_id="hospital-b").list_by_study(uid)) == 0


class TestAuditRepositoryIsolation:
    @pytest.mark.asyncio
    async def test_list_by_entity_scoped_to_tenant(self, db_session):
        entry = AuditEntry(entity_type="job", entity_id="job-1")
        await PgAuditRepository(db_session, tenant_id="hospital-a").save(entry)
        await db_session.commit()

        same_tenant = await PgAuditRepository(db_session, tenant_id="hospital-a").list_by_entity("job", "job-1")
        assert len(same_tenant) == 1

        other_tenant = await PgAuditRepository(db_session, tenant_id="hospital-b").list_by_entity("job", "job-1")
        assert len(other_tenant) == 0


class TestTenantRepository:
    @pytest.mark.asyncio
    async def test_create_get_and_list(self, db_session):
        repo = PgTenantRepository(db_session)
        await repo.save(Tenant(id="hospital-a", name="Hospital A", slug="hospital-a"))
        await db_session.commit()

        by_id = await repo.get_by_id("hospital-a")
        assert by_id is not None and by_id.slug == "hospital-a"

        by_slug = await repo.get_by_slug("hospital-a")
        assert by_slug is not None and by_slug.id == "hospital-a"

        assert await repo.get_by_slug("no-such-slug") is None
        assert len(await repo.list_all()) == 1

    @pytest.mark.asyncio
    async def test_update_status_and_features(self, db_session):
        repo = PgTenantRepository(db_session)
        tenant = await repo.save(Tenant(id="hospital-a", name="Hospital A", slug="hospital-a"))
        await db_session.commit()

        tenant.status = "suspended"
        tenant.features = ["brain_mri"]
        await repo.update(tenant)
        await db_session.commit()

        refetched = await repo.get_by_id("hospital-a")
        assert refetched.status == "suspended"
        assert refetched.features == ["brain_mri"]
