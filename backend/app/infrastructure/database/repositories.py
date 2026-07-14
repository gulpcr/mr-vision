from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditAction, JobStatus, QAFlag
from app.domain.interfaces import (
    AuditRepository,
    JobRepository,
    PendingStudyTenantRepository,
    PlanFeatureRepository,
    ResultRepository,
    SeriesRepository,
    StudyRepository,
    TenantApiKeyRepository,
    TenantRepository,
    UseCaseRegistryRepository,
)
from app.domain.models import (
    AuditEntry,
    JobRun,
    PendingStudyTenant,
    Result,
    ResultArtifact,
    RoutingRule,
    Series,
    Study,
    Tenant,
    TenantApiKey,
    UseCase,
)
from app.infrastructure.database.models import (
    AuditLogRecord,
    JobRunRecord,
    PendingStudyTenantRecord,
    PlanFeatureRecord,
    ResultRecord,
    SeriesRecord,
    StudyRecord,
    TenantApiKeyRecord,
    TenantRecord,
    UseCaseRegistryRecord,
)


class PgStudyRepository(StudyRepository):
    def __init__(self, session: AsyncSession, tenant_id: str | None = None):
        self._session = session
        self._tenant_id = tenant_id

    def _scope(self, stmt):
        if self._tenant_id:
            stmt = stmt.where(StudyRecord.tenant_id == self._tenant_id)
        return stmt

    async def save(self, study: Study) -> Study:
        record = StudyRecord(
            study_instance_uid=study.study_instance_uid,
            patient_id=study.patient_id,
            patient_name=study.patient_name,
            patient_sex=study.patient_sex,
            patient_age=study.patient_age,
            patient_weight_kg=study.patient_weight_kg,
            patient_height_cm=study.patient_height_cm,
            study_date=study.study_date,
            study_description=study.study_description,
            accession_number=study.accession_number,
            referring_physician=study.referring_physician,
            body_part_examined=study.body_part_examined.value if study.body_part_examined else None,
            modality=study.modality,
            institution_name=study.institution_name,
            orthanc_id=study.orthanc_id,
            tenant_id=study.tenant_id if study.tenant_id != "default" else (self._tenant_id or "default"),
        )
        await self._session.merge(record)
        await self._session.flush()
        return study

    async def get_by_uid(self, study_instance_uid: str) -> Study | None:
        stmt = select(StudyRecord).where(
            StudyRecord.study_instance_uid == study_instance_uid
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_studies(
        self, offset: int = 0, limit: int = 50, filters: dict[str, Any] | None = None
    ) -> list[Study]:
        stmt = select(StudyRecord).order_by(StudyRecord.created_at.desc())
        stmt = self._apply_filters(stmt, filters)
        stmt = self._scope(stmt)
        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        stmt = select(func.count()).select_from(StudyRecord)
        stmt = self._apply_filters(stmt, filters)
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def update(self, study: Study) -> Study:
        stmt = (
            update(StudyRecord)
            .where(StudyRecord.study_instance_uid == study.study_instance_uid)
            .values(
                patient_id=study.patient_id,
                patient_name=study.patient_name,
                patient_sex=study.patient_sex,
                patient_age=study.patient_age,
                patient_weight_kg=study.patient_weight_kg,
                patient_height_cm=study.patient_height_cm,
                study_date=study.study_date,
                study_description=study.study_description,
                accession_number=study.accession_number,
                referring_physician=study.referring_physician,
                body_part_examined=study.body_part_examined.value if study.body_part_examined else None,
                modality=study.modality,
                institution_name=study.institution_name,
                orthanc_id=study.orthanc_id,
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        stmt = self._scope(stmt)
        await self._session.execute(stmt)
        await self._session.flush()
        return study

    def _apply_filters(self, stmt, filters: dict[str, Any] | None):
        if not filters:
            return stmt
        if "body_part_examined" in filters:
            stmt = stmt.where(StudyRecord.body_part_examined == filters["body_part_examined"])
        if "modality" in filters:
            stmt = stmt.where(StudyRecord.modality == filters["modality"])
        if "patient_id" in filters:
            stmt = stmt.where(StudyRecord.patient_id == filters["patient_id"])
        if "study_date_from" in filters:
            stmt = stmt.where(StudyRecord.study_date >= filters["study_date_from"])
        if "study_date_to" in filters:
            stmt = stmt.where(StudyRecord.study_date <= filters["study_date_to"])
        return stmt

    @staticmethod
    def _to_domain(record: StudyRecord) -> Study:
        from app.domain.enums import BodyPart

        body_part = None
        if record.body_part_examined:
            try:
                body_part = BodyPart(record.body_part_examined)
            except ValueError:
                body_part = None

        return Study(
            study_instance_uid=record.study_instance_uid,
            patient_id=record.patient_id,
            patient_name=record.patient_name,
            patient_sex=record.patient_sex,
            patient_age=record.patient_age,
            patient_weight_kg=record.patient_weight_kg,
            patient_height_cm=record.patient_height_cm,
            study_date=record.study_date,
            study_description=record.study_description,
            accession_number=record.accession_number,
            referring_physician=record.referring_physician,
            body_part_examined=body_part,
            modality=record.modality,
            institution_name=record.institution_name,
            orthanc_id=record.orthanc_id,
            tenant_id=getattr(record, "tenant_id", None) or "default",
            reading_status=getattr(record, "reading_status", "unread") or "unread",
            assigned_to=getattr(record, "assigned_to", None),
            assigned_to_username=getattr(record, "assigned_to_username", None),
            assigned_at=getattr(record, "assigned_at", None),
            reported_at=getattr(record, "reported_at", None),
            signed_at=getattr(record, "signed_at", None),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class PgSeriesRepository(SeriesRepository):
    def __init__(self, session: AsyncSession, tenant_id: str | None = None):
        self._session = session
        self._tenant_id = tenant_id

    def _scope(self, stmt):
        if self._tenant_id:
            stmt = stmt.where(SeriesRecord.tenant_id == self._tenant_id)
        return stmt

    async def save(self, series: Series) -> Series:
        record = SeriesRecord(
            series_instance_uid=series.series_instance_uid,
            study_instance_uid=series.study_instance_uid,
            series_number=series.series_number,
            series_description=series.series_description,
            modality=series.modality,
            body_part_examined=series.body_part_examined,
            protocol_name=series.protocol_name,
            num_instances=series.num_instances,
            slice_thickness=series.slice_thickness,
            pixel_spacing=list(series.pixel_spacing) if series.pixel_spacing else None,
            image_orientation=series.image_orientation,
            orthanc_id=series.orthanc_id,
            dicom_tags=series.dicom_tags,
            tenant_id=series.tenant_id if series.tenant_id != "default" else (self._tenant_id or "default"),
        )
        await self._session.merge(record)
        await self._session.flush()
        return series

    async def get_by_uid(self, series_instance_uid: str) -> Series | None:
        stmt = select(SeriesRecord).where(
            SeriesRecord.series_instance_uid == series_instance_uid
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_by_study(self, study_instance_uid: str) -> list[Series]:
        stmt = (
            select(SeriesRecord)
            .where(SeriesRecord.study_instance_uid == study_instance_uid)
            .order_by(SeriesRecord.series_number)
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def save_many(self, series_list: list[Series]) -> list[Series]:
        for s in series_list:
            await self.save(s)
        return series_list

    @staticmethod
    def _to_domain(record: SeriesRecord) -> Series:
        pixel_spacing = None
        if record.pixel_spacing and len(record.pixel_spacing) == 2:
            pixel_spacing = tuple(record.pixel_spacing)
        return Series(
            series_instance_uid=record.series_instance_uid,
            study_instance_uid=record.study_instance_uid,
            series_number=record.series_number,
            series_description=record.series_description,
            modality=record.modality,
            body_part_examined=record.body_part_examined,
            protocol_name=record.protocol_name,
            num_instances=record.num_instances,
            slice_thickness=record.slice_thickness,
            pixel_spacing=pixel_spacing,
            image_orientation=record.image_orientation,
            orthanc_id=record.orthanc_id,
            dicom_tags=record.dicom_tags or {},
            tenant_id=getattr(record, "tenant_id", None) or "default",
            created_at=record.created_at,
        )


class PgJobRepository(JobRepository):
    def __init__(self, session: AsyncSession, tenant_id: str | None = None):
        self._session = session
        self._tenant_id = tenant_id

    def _scope(self, stmt):
        if self._tenant_id:
            stmt = stmt.where(JobRunRecord.tenant_id == self._tenant_id)
        return stmt

    async def save(self, job: JobRun) -> JobRun:
        record = JobRunRecord(
            id=job.id,
            study_instance_uid=job.study_instance_uid,
            usecase_name=job.usecase_name,
            status=job.status.value,
            priority=job.priority,
            progress=job.progress,
            status_message=job.status_message,
            worker_id=job.worker_id,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error_detail=job.error_detail,
            retry_count=job.retry_count,
            tenant_id=job.tenant_id if job.tenant_id != "default" else (self._tenant_id or "default"),
        )
        self._session.add(record)
        await self._session.flush()
        return job

    async def get_by_id(self, job_id: str) -> JobRun | None:
        stmt = select(JobRunRecord).where(JobRunRecord.id == job_id)
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_by_study(self, study_instance_uid: str) -> list[JobRun]:
        stmt = (
            select(JobRunRecord)
            .where(JobRunRecord.study_instance_uid == study_instance_uid)
            .order_by(JobRunRecord.created_at.desc())
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def update(self, job: JobRun) -> JobRun:
        stmt = (
            update(JobRunRecord)
            .where(JobRunRecord.id == job.id)
            .values(
                status=job.status.value,
                progress=job.progress,
                status_message=job.status_message,
                worker_id=job.worker_id,
                started_at=job.started_at,
                completed_at=job.completed_at,
                error_detail=job.error_detail,
                retry_count=job.retry_count,
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        stmt = self._scope(stmt)
        await self._session.execute(stmt)
        await self._session.flush()
        return job

    async def list_jobs(
        self, offset: int = 0, limit: int = 50, filters: dict[str, Any] | None = None
    ) -> list[JobRun]:
        stmt = select(JobRunRecord).order_by(JobRunRecord.created_at.desc())
        if filters:
            if "status" in filters:
                stmt = stmt.where(JobRunRecord.status == filters["status"])
            if "usecase_name" in filters:
                stmt = stmt.where(JobRunRecord.usecase_name == filters["usecase_name"])
        stmt = self._scope(stmt)
        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    @staticmethod
    def _to_domain(record: JobRunRecord) -> JobRun:
        return JobRun(
            id=record.id,
            study_instance_uid=record.study_instance_uid,
            usecase_name=record.usecase_name,
            status=JobStatus(record.status),
            priority=record.priority,
            progress=record.progress,
            status_message=record.status_message or "",
            worker_id=record.worker_id,
            started_at=record.started_at,
            completed_at=record.completed_at,
            error_detail=record.error_detail,
            retry_count=getattr(record, "retry_count", 0),
            tenant_id=getattr(record, "tenant_id", None) or "default",
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class PgResultRepository(ResultRepository):
    def __init__(self, session: AsyncSession, tenant_id: str | None = None):
        self._session = session
        self._tenant_id = tenant_id

    def _scope(self, stmt):
        if self._tenant_id:
            stmt = stmt.where(ResultRecord.tenant_id == self._tenant_id)
        return stmt

    async def save(self, result: Result) -> Result:
        # Mark previous latest version as not-latest
        prev_stmt = (
            select(ResultRecord)
            .where(
                ResultRecord.study_instance_uid == result.study_instance_uid,
                ResultRecord.usecase_name == result.usecase_name,
                ResultRecord.is_latest == True,
            )
            .order_by(ResultRecord.version.desc())
        )
        prev_result = await self._session.execute(prev_stmt)
        prev_record = prev_result.scalar_one_or_none()

        next_version = 1
        if prev_record:
            next_version = prev_record.version + 1
            prev_record.is_latest = False

        record = ResultRecord(
            id=result.id,
            study_instance_uid=result.study_instance_uid,
            usecase_name=result.usecase_name,
            job_id=result.job_id,
            summary=result.summary,
            measurements=result.measurements,
            qa_flags=[f.value if isinstance(f, QAFlag) else f for f in result.qa_flags],
            qa_details=result.qa_details,
            model_version=result.model_version,
            model_checksum=result.model_checksum,
            artifacts=[
                {
                    "name": a.name,
                    "artifact_type": a.artifact_type,
                    "storage_path": a.storage_path,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                }
                for a in result.artifacts
            ],
            version=next_version,
            is_latest=True,
            tenant_id=result.tenant_id if result.tenant_id != "default" else (self._tenant_id or "default"),
        )
        self._session.add(record)
        await self._session.flush()
        result.version = next_version
        result.is_latest = True
        return result

    async def get_by_study_and_usecase(
        self, study_instance_uid: str, usecase_name: str
    ) -> Result | None:
        stmt = select(ResultRecord).where(
            ResultRecord.study_instance_uid == study_instance_uid,
            ResultRecord.usecase_name == usecase_name,
            ResultRecord.is_latest == True,
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def get_by_study_usecase_version(
        self, study_instance_uid: str, usecase_name: str, version: int
    ) -> Result | None:
        stmt = select(ResultRecord).where(
            ResultRecord.study_instance_uid == study_instance_uid,
            ResultRecord.usecase_name == usecase_name,
            ResultRecord.version == version,
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_versions(
        self, study_instance_uid: str, usecase_name: str
    ) -> list[Result]:
        stmt = (
            select(ResultRecord)
            .where(
                ResultRecord.study_instance_uid == study_instance_uid,
                ResultRecord.usecase_name == usecase_name,
            )
            .order_by(ResultRecord.version.desc())
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def get_by_id(self, result_id: str) -> Result | None:
        stmt = select(ResultRecord).where(ResultRecord.id == result_id)
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_by_study(self, study_instance_uid: str) -> list[Result]:
        stmt = (
            select(ResultRecord)
            .where(
                ResultRecord.study_instance_uid == study_instance_uid,
                ResultRecord.is_latest == True,
            )
            .order_by(ResultRecord.created_at.desc())
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    @staticmethod
    def _to_domain(record: ResultRecord) -> Result:
        artifacts = []
        for a in (record.artifacts or []):
            artifacts.append(
                ResultArtifact(
                    name=a["name"],
                    artifact_type=a["artifact_type"],
                    storage_path=a["storage_path"],
                    content_type=a.get("content_type", "application/octet-stream"),
                    size_bytes=a.get("size_bytes", 0),
                )
            )
        qa_flags = []
        for f in (record.qa_flags or []):
            try:
                qa_flags.append(QAFlag(f))
            except ValueError:
                pass
        return Result(
            id=record.id,
            study_instance_uid=record.study_instance_uid,
            usecase_name=record.usecase_name,
            job_id=record.job_id or "",
            summary=record.summary or {},
            measurements=record.measurements or {},
            qa_flags=qa_flags,
            qa_details=record.qa_details or {},
            model_version=record.model_version,
            model_checksum=record.model_checksum,
            artifacts=artifacts,
            tenant_id=getattr(record, "tenant_id", None) or "default",
            version=getattr(record, "version", 1),
            is_latest=getattr(record, "is_latest", True),
            created_at=record.created_at,
        )


class PgUseCaseRegistryRepository(UseCaseRegistryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, usecase: UseCase) -> UseCase:
        record = UseCaseRegistryRecord(
            name=usecase.name,
            version=usecase.version,
            supported_body_parts=usecase.supported_body_parts,
            required_sequences=usecase.required_sequences,
            model_type=usecase.model_type,
            enabled=usecase.enabled,
            module_path=usecase.module_path,
            description=usecase.description,
        )
        merged = await self._session.merge(record)
        await self._session.flush()
        return usecase

    async def get_by_name(self, name: str) -> UseCase | None:
        stmt = select(UseCaseRegistryRecord).where(UseCaseRegistryRecord.name == name)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_all(self) -> list[UseCase]:
        stmt = select(UseCaseRegistryRecord).order_by(UseCaseRegistryRecord.name)
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def update(self, usecase: UseCase) -> UseCase:
        return await self.save(usecase)

    @staticmethod
    def _to_domain(record: UseCaseRegistryRecord) -> UseCase:
        return UseCase(
            name=record.name,
            version=record.version,
            supported_body_parts=record.supported_body_parts or [],
            required_sequences=record.required_sequences or [],
            model_type=record.model_type,
            enabled=record.enabled,
            module_path=record.module_path,
            description=record.description or "",
            registered_at=record.registered_at,
        )


class PgAuditRepository(AuditRepository):
    def __init__(self, session: AsyncSession, tenant_id: str | None = None):
        self._session = session
        self._tenant_id = tenant_id

    def _scope(self, stmt):
        if self._tenant_id:
            stmt = stmt.where(AuditLogRecord.tenant_id == self._tenant_id)
        return stmt

    async def save(self, entry: AuditEntry) -> AuditEntry:
        from app.config import derive_secret
        from app.domain.audit_chain import compute_audit_row_hash

        secret = derive_secret("audit-chain-v1")
        action_value = entry.action.value if isinstance(entry.action, AuditAction) else entry.action
        tenant_id = entry.tenant_id if entry.tenant_id != "default" else (self._tenant_id or "default")

        # Lock the current chain tail so concurrent writers serialize on it —
        # without this, two concurrent audit writes could both read the same
        # prev_hash and fork the chain instead of extending it linearly.
        # (No-op on SQLite, which has no row-level FOR UPDATE — fine for
        # single-writer tests; Postgres is where concurrent writes happen.)
        tail_stmt = (
            select(AuditLogRecord.seq, AuditLogRecord.row_hash)
            .where(AuditLogRecord.seq.isnot(None))
            .order_by(AuditLogRecord.seq.desc())
            .limit(1)
            .with_for_update()
        )
        tail = (await self._session.execute(tail_stmt)).first()
        next_seq = (tail.seq + 1) if tail else 1
        prev_hash = tail.row_hash if tail else None

        row_hash = compute_audit_row_hash(
            secret=secret,
            seq=next_seq,
            action=action_value,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            actor=entry.actor,
            details=entry.details,
            tenant_id=tenant_id,
            prev_hash=prev_hash,
        )

        record = AuditLogRecord(
            id=entry.id,
            action=action_value,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            actor=entry.actor,
            details=entry.details,
            tenant_id=tenant_id,
            seq=next_seq,
            prev_hash=prev_hash,
            row_hash=row_hash,
        )
        self._session.add(record)
        await self._session.flush()
        return entry

    async def list_by_entity(
        self, entity_type: str, entity_id: str, limit: int = 100
    ) -> list[AuditEntry]:
        stmt = (
            select(AuditLogRecord)
            .where(
                AuditLogRecord.entity_type == entity_type,
                AuditLogRecord.entity_id == entity_id,
            )
            .order_by(AuditLogRecord.timestamp.desc())
            .limit(limit)
        )
        stmt = self._scope(stmt)
        result = await self._session.execute(stmt)
        entries = []
        for r in result.scalars().all():
            try:
                action = AuditAction(r.action)
            except ValueError:
                action = r.action
            entries.append(
                AuditEntry(
                    id=r.id,
                    action=action,
                    entity_type=r.entity_type,
                    entity_id=r.entity_id,
                    actor=r.actor,
                    details=r.details or {},
                    tenant_id=getattr(r, "tenant_id", None) or "default",
                    timestamp=r.timestamp,
                )
            )
        return entries


class PgTenantRepository(TenantRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, tenant: Tenant) -> Tenant:
        record = TenantRecord(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            status=tenant.status,
            plan=tenant.plan,
            features=list(tenant.features),
        )
        self._session.add(record)
        await self._session.flush()
        return tenant

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        stmt = select(TenantRecord).where(TenantRecord.id == tenant_id)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = select(TenantRecord).where(TenantRecord.slug == slug)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_all(self) -> list[Tenant]:
        stmt = select(TenantRecord).order_by(TenantRecord.created_at.desc())
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def update(self, tenant: Tenant) -> Tenant:
        stmt = (
            update(TenantRecord)
            .where(TenantRecord.id == tenant.id)
            .values(
                name=tenant.name,
                is_active=tenant.is_active,
                status=tenant.status,
                plan=tenant.plan,
                features=list(tenant.features),
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return tenant

    @staticmethod
    def _to_domain(record: TenantRecord) -> Tenant:
        return Tenant(
            id=record.id,
            name=record.name,
            slug=record.slug,
            is_active=record.is_active,
            status=getattr(record, "status", None) or "active",
            plan=getattr(record, "plan", None) or "starter",
            features=list(getattr(record, "features", None) or []),
            created_at=record.created_at,
        )


class PgTenantApiKeyRepository(TenantApiKeyRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, key: TenantApiKey) -> TenantApiKey:
        record = TenantApiKeyRecord(
            id=key.id,
            tenant_id=key.tenant_id,
            name=key.name,
            key_hash=key.key_hash,
            prefix=key.prefix,
            scopes=list(key.scopes),
            expires_at=key.expires_at,
            is_active=key.is_active,
        )
        self._session.add(record)
        await self._session.flush()
        return key

    async def get_by_id(self, key_id: str) -> TenantApiKey | None:
        stmt = select(TenantApiKeyRecord).where(TenantApiKeyRecord.id == key_id)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def get_by_hash(self, key_hash: str) -> TenantApiKey | None:
        stmt = select(TenantApiKeyRecord).where(
            TenantApiKeyRecord.key_hash == key_hash,
            TenantApiKeyRecord.revoked_at.is_(None),
        )
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def list_by_tenant(self, tenant_id: str) -> list[TenantApiKey]:
        stmt = (
            select(TenantApiKeyRecord)
            .where(TenantApiKeyRecord.tenant_id == tenant_id)
            .order_by(TenantApiKeyRecord.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return [self._to_domain(r) for r in result.scalars().all()]

    async def update(self, key: TenantApiKey) -> TenantApiKey:
        stmt = (
            update(TenantApiKeyRecord)
            .where(TenantApiKeyRecord.id == key.id)
            .values(
                is_active=key.is_active,
                last_used_at=key.last_used_at,
                revoked_at=key.revoked_at,
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return key

    @staticmethod
    def _to_domain(record: TenantApiKeyRecord) -> TenantApiKey:
        return TenantApiKey(
            id=record.id,
            tenant_id=record.tenant_id,
            name=record.name,
            key_hash=record.key_hash,
            prefix=record.prefix,
            scopes=list(record.scopes or []),
            expires_at=record.expires_at,
            is_active=record.is_active,
            last_used_at=record.last_used_at,
            revoked_at=record.revoked_at,
            created_at=record.created_at,
        )


class PgPendingStudyTenantRepository(PendingStudyTenantRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_study_uid(self, study_instance_uid: str) -> PendingStudyTenant | None:
        stmt = select(PendingStudyTenantRecord).where(
            PendingStudyTenantRecord.study_instance_uid == study_instance_uid
        )
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self._to_domain(record)

    async def register(self, mapping: PendingStudyTenant) -> PendingStudyTenant:
        existing = await self.get_by_study_uid(mapping.study_instance_uid)
        if existing is not None:
            if existing.tenant_id != mapping.tenant_id:
                raise ValueError(
                    f"Study {mapping.study_instance_uid} is already registered "
                    f"to a different workspace"
                )
            return existing

        record = PendingStudyTenantRecord(
            study_instance_uid=mapping.study_instance_uid,
            tenant_id=mapping.tenant_id,
            api_key_id=mapping.api_key_id,
        )
        self._session.add(record)
        await self._session.flush()
        return mapping

    async def consume(self, study_instance_uid: str) -> None:
        await self._session.execute(
            delete(PendingStudyTenantRecord).where(
                PendingStudyTenantRecord.study_instance_uid == study_instance_uid
            )
        )
        await self._session.flush()

    @staticmethod
    def _to_domain(record: PendingStudyTenantRecord) -> PendingStudyTenant:
        return PendingStudyTenant(
            study_instance_uid=record.study_instance_uid,
            tenant_id=record.tenant_id,
            api_key_id=record.api_key_id,
            created_at=record.created_at,
        )


class PgPlanFeatureRepository(PlanFeatureRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_by_plan(self, plan_name: str) -> list[str]:
        stmt = select(PlanFeatureRecord.feature_key).where(
            PlanFeatureRecord.plan_name == plan_name
        )
        result = await self._session.execute(stmt)
        return [row[0] for row in result.all()]

    async def set_plan_features(self, plan_name: str, feature_keys: list[str]) -> None:
        await self._session.execute(
            delete(PlanFeatureRecord).where(PlanFeatureRecord.plan_name == plan_name)
        )
        for key in feature_keys:
            self._session.add(PlanFeatureRecord(plan_name=plan_name, feature_key=key))
        await self._session.flush()

    async def list_all_plans(self) -> dict[str, list[str]]:
        stmt = select(PlanFeatureRecord.plan_name, PlanFeatureRecord.feature_key)
        result = await self._session.execute(stmt)
        plans: dict[str, list[str]] = {}
        for plan_name, feature_key in result.all():
            plans.setdefault(plan_name, []).append(feature_key)
        return plans
