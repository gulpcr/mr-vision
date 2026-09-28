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
    """Manages data retention policies and purge/archive operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

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
        from app.infrastructure.database.models import (
            RetentionPolicyRecord,
            StudyRecord,
            JobRunRecord,
            ResultRecord,
            AuditLogRecord,
        )

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

            cutoff = utcnow() - timedelta(days=policy.max_age_days)

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
                    del_stmt = delete(model).where(*in_scope)
                    await self._session.execute(del_stmt)
                    logger.info(
                        "retention_purged",
                        entity_type=policy.entity_type,
                        count=count,
                        policy=policy.name,
                    )

                totals[policy.entity_type] = totals.get(policy.entity_type, 0) + count

        await self._session.flush()
        return totals
