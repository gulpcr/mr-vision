from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
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

        Each policy's purge is scoped to that policy's OWN tenant_id — a
        tenant's retention policy must only ever touch that tenant's data.
        Previously the purge filtered only by age, so one tenant's policy
        could delete every OTHER tenant's records of the same entity_type
        that happened to be old enough — a real cross-tenant data-loss bug.

        tenant_id here restricts which POLICIES run (e.g. "apply just this
        tenant's policies"); omit to run every active policy, each still
        confined to its own tenant's rows by the fix above.
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

            # created_at is a naive TIMESTAMP column (stores UTC) — asyncpg
            # rejects binding a tz-aware datetime to it.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=policy.max_age_days)).replace(
                tzinfo=None
            )
            policy_tenant_id = policy.tenant_id or "default"

            if hasattr(model, "created_at"):
                count_stmt = select(func.count()).select_from(model).where(
                    model.created_at < cutoff
                )
                if hasattr(model, "tenant_id"):
                    count_stmt = count_stmt.where(model.tenant_id == policy_tenant_id)
                count_result = await self._session.execute(count_stmt)
                count = count_result.scalar_one()

                if policy.action == "delete" and count > 0:
                    del_stmt = delete(model).where(model.created_at < cutoff)
                    if hasattr(model, "tenant_id"):
                        del_stmt = del_stmt.where(model.tenant_id == policy_tenant_id)
                    await self._session.execute(del_stmt)
                    logger.info(
                        "retention_purged",
                        entity_type=policy.entity_type,
                        tenant_id=policy_tenant_id,
                        count=count,
                        policy=policy.name,
                    )

                    from app.domain.enums import AuditAction
                    from app.domain.models import AuditEntry
                    from app.infrastructure.database.repositories import PgAuditRepository
                    # Note: if this policy's entity_type is "audit" itself, the
                    # deleted rows may include chained entries — that opens a
                    # detectable gap in AuditIntegrityService.verify_chain().
                    # That's intentional, not a bug: this entry is the durable
                    # record of WHY the gap exists, distinguishing an
                    # authorized retention purge from silent tampering.
                    await PgAuditRepository(self._session).save(AuditEntry(
                        action=AuditAction.DATA_PURGED,
                        entity_type=policy.entity_type,
                        entity_id=policy.id,
                        actor="system",
                        details={"policy": policy.name, "count": count},
                        tenant_id=policy_tenant_id,
                    ))

                totals[policy.entity_type] = totals.get(policy.entity_type, 0) + count

        await self._session.flush()
        return totals
