from __future__ import annotations

import uuid
from datetime import timedelta

from app.domain.models import utcnow
from typing import Any

import structlog
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class RetentionService:
    """Manages data retention policies and purge/archive operations.

    A purge is only real if it reaches every copy (HIPAA 164.310(d)(2) disposal): deleting
    a study also removes its images from the PACS and its AI artifacts from the object
    store. Those copies go first; a study whose copies could not all be removed keeps its
    database row, so the next run retries instead of leaving untracked PHI behind.
    """

    def __init__(self, session: AsyncSession, artifact_store=None, pacs=None):
        self._session = session
        self._store = artifact_store
        self._pacs = pacs

    async def create_policy(
        self,
        name: str,
        entity_type: str,
        max_age_days: int = 365,
        action: str = "archive",
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import RetentionPolicyRecord

        record = RetentionPolicyRecord(
            id=str(uuid.uuid4()),
            name=name,
            entity_type=entity_type,
            max_age_days=max_age_days,
            action=action,
            is_active=True,
            tenant_id=tenant_id,
        )
        self._session.add(record)
        await self._session.flush()
        return {
            "id": record.id,
            "name": name,
            "entity_type": entity_type,
            "max_age_days": max_age_days,
            "action": action,
            "is_active": True,
        }

    async def list_policies(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import RetentionPolicyRecord

        stmt = select(RetentionPolicyRecord).order_by(RetentionPolicyRecord.created_at.desc())
        if tenant_id:
            stmt = stmt.where(RetentionPolicyRecord.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "name": r.name,
                "entity_type": r.entity_type,
                "max_age_days": r.max_age_days,
                "action": r.action,
                "is_active": r.is_active,
                "tenant_id": r.tenant_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    async def delete_policy(self, policy_id: str, tenant_id: str | None = None) -> bool:
        from app.infrastructure.database.models import RetentionPolicyRecord

        stmt = delete(RetentionPolicyRecord).where(RetentionPolicyRecord.id == policy_id)
        if tenant_id:
            stmt = stmt.where(RetentionPolicyRecord.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def apply_policies(self, tenant_id: str | None = None) -> dict[str, int]:
        """Apply active retention policies. Returns counts of affected records.

        Each policy only ever purges rows of *its own* tenant. Previously a policy's
        delete had no tenant predicate, so one tenant's policy deleted matching studies,
        jobs and results of every tenant. ``tenant_id`` limits which policies run
        (None = all tenants' policies — the Celery beat job, under a platform DB scope).

        ``audit`` policies are reported but never purge: the audit log is one global hash
        chain (alembic 040/044), and deleting rows from it breaks chain verification.
        """
        from app.config import get_settings
        from app.infrastructure.database.models import (
            RetentionPolicyRecord,
            StudyRecord,
            JobRunRecord,
            ResultRecord,
            AuditLogRecord,
        )

        settings = get_settings()
        if not settings.retention_enabled:
            logger.info("retention_skipped_disabled")
            return {}

        stmt = select(RetentionPolicyRecord).where(
            RetentionPolicyRecord.is_active == True
        )
        if tenant_id:
            stmt = stmt.where(RetentionPolicyRecord.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        policies = result.scalars().all()

        totals = {}
        entity_map = {
            "study": StudyRecord,
            "job": JobRunRecord,
            "result": ResultRecord,
            "audit": AuditLogRecord,
        }

        for policy in policies:
            model = entity_map.get(policy.entity_type)
            if not model:
                continue

            max_age_days = policy.max_age_days
            if policy.action == "delete" and policy.entity_type in ("study", "result"):
                # Medical-record retention floor: never purge clinical data younger than it.
                max_age_days = max(max_age_days, settings.retention_min_days)
            cutoff = utcnow() - timedelta(days=max_age_days)

            if hasattr(model, "created_at"):
                in_scope = (model.created_at < cutoff, model.tenant_id == policy.tenant_id)
                count_stmt = select(func.count()).select_from(model).where(*in_scope)
                count_result = await self._session.execute(count_stmt)
                count = count_result.scalar_one()

                if policy.action == "delete" and policy.entity_type == "audit":
                    logger.warning(
                        "retention_audit_purge_skipped",
                        policy=policy.name,
                        reason="audit_log is a hash chain; deleting rows breaks verification",
                    )
                elif policy.action == "delete" and count > 0:
                    if policy.entity_type in ("study", "result"):
                        count = await self._purge_with_copies(policy, model, in_scope)
                    else:
                        await self._session.execute(delete(model).where(*in_scope))
                    logger.info(
                        "retention_purged",
                        entity_type=policy.entity_type,
                        count=count,
                        policy=policy.name,
                    )
                    await self._audit_purge(policy, count)

                totals[policy.entity_type] = totals.get(policy.entity_type, 0) + count

        await self._session.flush()
        return totals

    async def _purge_with_copies(self, policy, model, in_scope) -> int:
        """Delete expired studies/results together with their PACS images and stored
        artifacts. Returns how many rows were actually purged."""
        from app.infrastructure.database.models import ResultRecord, StudyRecord

        if model is StudyRecord:
            studies = (await self._session.execute(select(StudyRecord).where(*in_scope))).scalars().all()
            purged = 0
            for study in studies:
                results = (await self._session.execute(select(ResultRecord).where(
                    ResultRecord.study_instance_uid == study.study_instance_uid,
                    ResultRecord.tenant_id == policy.tenant_id,
                ))).scalars().all()
                if not await self._remove_copies(results, study.study_instance_uid):
                    continue
                await self._session.execute(delete(ResultRecord).where(
                    ResultRecord.study_instance_uid == study.study_instance_uid,
                    ResultRecord.tenant_id == policy.tenant_id,
                ))
                await self._session.execute(delete(StudyRecord).where(
                    StudyRecord.study_instance_uid == study.study_instance_uid,
                    StudyRecord.tenant_id == policy.tenant_id,
                ))
                purged += 1
            return purged

        results = (await self._session.execute(select(ResultRecord).where(*in_scope))).scalars().all()
        purged = 0
        for result in results:
            if not await self._remove_copies([result], None):
                continue
            await self._session.execute(delete(ResultRecord).where(ResultRecord.id == result.id))
            purged += 1
        return purged

    async def _remove_copies(self, results, study_uid: str | None) -> bool:
        """Delete artifacts of ``results`` and (for a study) its PACS images."""
        try:
            if self._store is not None:
                for r in results:
                    for a in r.artifacts or []:
                        path = a.get("storage_path") if isinstance(a, dict) else None
                        if path:
                            await self._store.delete(path)
            if study_uid and self._pacs is not None:
                await self._pacs.delete_study_by_uid(study_uid)
            return True
        except Exception as exc:
            logger.warning("retention_copy_delete_failed", study=study_uid, error=type(exc).__name__)
            return False

    async def _audit_purge(self, policy, count: int) -> None:
        from app.domain.enums import AuditAction
        from app.domain.models import AuditEntry
        from app.infrastructure.database.repositories import PgAuditRepository

        await PgAuditRepository(self._session, tenant_id=policy.tenant_id).save(AuditEntry(
            action=AuditAction.DATA_PURGED, entity_type=policy.entity_type,
            entity_id=policy.id, actor="retention",
            details={"policy": policy.name, "count": count, "max_age_days": policy.max_age_days,
                     "images_and_artifacts_removed": self._store is not None},
            tenant_id=policy.tenant_id,
        ))
