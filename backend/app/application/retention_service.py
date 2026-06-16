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

    async def delete_policy(self, policy_id: str) -> bool:
        from app.infrastructure.database.models import RetentionPolicyRecord

        stmt = delete(RetentionPolicyRecord).where(RetentionPolicyRecord.id == policy_id)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def apply_policies(self) -> dict[str, int]:
        """Apply all active retention policies. Returns counts of affected records."""
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

            cutoff = datetime.now(timezone.utc) - timedelta(days=policy.max_age_days)

            if hasattr(model, "created_at"):
                count_stmt = select(func.count()).select_from(model).where(
                    model.created_at < cutoff
                )
                count_result = await self._session.execute(count_stmt)
                count = count_result.scalar_one()

                if policy.action == "delete" and count > 0:
                    del_stmt = delete(model).where(model.created_at < cutoff)
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
