from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class BatchUploadService:
    """Manages multi-study batch uploads with progress tracking."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_batch(
        self,
        name: str,
        study_uids: list[str],
        created_by: str = "",
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import BatchUploadRecord, BatchUploadItemRecord

        batch_id = str(uuid.uuid4())
        batch = BatchUploadRecord(
            id=batch_id,
            name=name,
            total_items=len(study_uids),
            completed_items=0,
            failed_items=0,
            status="pending",
            created_by=created_by,
            tenant_id=tenant_id,
        )
        self._session.add(batch)

        for uid in study_uids:
            item = BatchUploadItemRecord(
                id=str(uuid.uuid4()),
                batch_id=batch_id,
                study_instance_uid=uid,
                status="pending",
            )
            self._session.add(item)

        await self._session.flush()
        return {
            "id": batch_id,
            "name": name,
            "total_items": len(study_uids),
            "status": "pending",
        }

    async def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        from app.infrastructure.database.models import BatchUploadRecord, BatchUploadItemRecord

        stmt = select(BatchUploadRecord).where(BatchUploadRecord.id == batch_id)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            return None

        items_stmt = select(BatchUploadItemRecord).where(
            BatchUploadItemRecord.batch_id == batch_id
        ).order_by(BatchUploadItemRecord.created_at)
        items_result = await self._session.execute(items_stmt)
        items = [
            {
                "id": item.id,
                "study_instance_uid": item.study_instance_uid,
                "status": item.status,
                "error_detail": item.error_detail,
            }
            for item in items_result.scalars().all()
        ]

        return {
            "id": record.id,
            "name": record.name,
            "total_items": record.total_items,
            "completed_items": record.completed_items,
            "failed_items": record.failed_items,
            "status": record.status,
            "created_by": record.created_by,
            "items": items,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    async def list_batches(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import BatchUploadRecord

        stmt = select(BatchUploadRecord).order_by(BatchUploadRecord.created_at.desc())
        if tenant_id:
            stmt = stmt.where(BatchUploadRecord.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "name": r.name,
                "total_items": r.total_items,
                "completed_items": r.completed_items,
                "failed_items": r.failed_items,
                "status": r.status,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    async def update_item_status(
        self,
        batch_id: str,
        study_instance_uid: str,
        status: str,
        error_detail: str | None = None,
    ) -> None:
        from app.infrastructure.database.models import BatchUploadItemRecord, BatchUploadRecord

        stmt = (
            update(BatchUploadItemRecord)
            .where(
                BatchUploadItemRecord.batch_id == batch_id,
                BatchUploadItemRecord.study_instance_uid == study_instance_uid,
            )
            .values(status=status, error_detail=error_detail)
        )
        await self._session.execute(stmt)

        # Update batch counters
        batch_stmt = select(BatchUploadRecord).where(BatchUploadRecord.id == batch_id)
        batch_result = await self._session.execute(batch_stmt)
        batch = batch_result.scalar_one_or_none()
        if not batch:
            return

        if status == "completed":
            batch.completed_items = (batch.completed_items or 0) + 1
        elif status == "failed":
            batch.failed_items = (batch.failed_items or 0) + 1

        total_done = (batch.completed_items or 0) + (batch.failed_items or 0)
        if total_done >= batch.total_items:
            batch.status = "completed" if (batch.failed_items or 0) == 0 else "partial"
        else:
            batch.status = "in_progress"

        await self._session.flush()
