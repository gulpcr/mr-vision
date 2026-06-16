from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = structlog.get_logger(__name__)


class ActiveLearningService:
    """Manages the review queue for low-confidence predictions."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def evaluate_result(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result_id: str,
        confidence_score: float,
    ) -> bool:
        """Check if a result should be flagged for review.

        Returns True if added to review queue.
        """
        settings = get_settings()
        if not settings.active_learning_enabled:
            return False

        if confidence_score >= settings.confidence_threshold:
            return False

        from app.infrastructure.database.models import ReviewQueueRecord

        # Check if already in queue
        stmt = select(ReviewQueueRecord).where(
            ReviewQueueRecord.result_id == result_id
        )
        result = await self._session.execute(stmt)
        if result.scalar_one_or_none():
            return False

        record = ReviewQueueRecord(
            id=str(uuid.uuid4()),
            study_instance_uid=study_instance_uid,
            usecase_name=usecase_name,
            result_id=result_id,
            confidence_score=confidence_score,
            status="pending",
        )
        self._session.add(record)
        await self._session.flush()

        logger.info(
            "review_item_added",
            study_uid=study_instance_uid,
            usecase=usecase_name,
            confidence=confidence_score,
        )
        return True

    async def list_review_queue(
        self,
        status: str | None = None,
        usecase_name: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import ReviewQueueRecord

        stmt = select(ReviewQueueRecord).order_by(
            ReviewQueueRecord.confidence_score.asc(),
            ReviewQueueRecord.created_at.desc(),
        )
        if status:
            stmt = stmt.where(ReviewQueueRecord.status == status)
        if usecase_name:
            stmt = stmt.where(ReviewQueueRecord.usecase_name == usecase_name)
        stmt = stmt.offset(offset).limit(limit)

        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "study_instance_uid": r.study_instance_uid,
                "usecase_name": r.usecase_name,
                "result_id": r.result_id,
                "confidence_score": r.confidence_score,
                "status": r.status,
                "reviewer": r.reviewer,
                "review_notes": r.review_notes,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    async def get_review_item(self, review_id: str) -> dict[str, Any] | None:
        from app.infrastructure.database.models import ReviewQueueRecord

        stmt = select(ReviewQueueRecord).where(ReviewQueueRecord.id == review_id)
        result = await self._session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r:
            return None
        return {
            "id": r.id,
            "study_instance_uid": r.study_instance_uid,
            "usecase_name": r.usecase_name,
            "result_id": r.result_id,
            "confidence_score": r.confidence_score,
            "status": r.status,
            "reviewer": r.reviewer,
            "review_notes": r.review_notes,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    async def submit_review(
        self,
        review_id: str,
        status: str,
        reviewer: str,
        notes: str = "",
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import ReviewQueueRecord

        stmt = (
            update(ReviewQueueRecord)
            .where(ReviewQueueRecord.id == review_id)
            .values(
                status=status,
                reviewer=reviewer,
                review_notes=notes,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

        return await self.get_review_item(review_id)

    async def add_to_review_queue(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result_id: str,
        confidence_score: float,
    ) -> bool:
        """Unconditionally add a result to the review queue (bypass threshold check)."""
        from app.infrastructure.database.models import ReviewQueueRecord

        stmt = select(ReviewQueueRecord).where(ReviewQueueRecord.result_id == result_id)
        result = await self._session.execute(stmt)
        if result.scalar_one_or_none():
            return False

        record = ReviewQueueRecord(
            id=str(uuid.uuid4()),
            study_instance_uid=study_instance_uid,
            usecase_name=usecase_name,
            result_id=result_id,
            confidence_score=confidence_score,
            status="pending",
        )
        self._session.add(record)
        await self._session.flush()
        logger.info(
            "review_item_added_forced",
            study_uid=study_instance_uid,
            usecase=usecase_name,
            confidence=confidence_score,
        )
        return True

    async def get_queue_stats(self) -> dict[str, int]:
        from app.infrastructure.database.models import ReviewQueueRecord
        from sqlalchemy import func

        stmt = (
            select(
                ReviewQueueRecord.status,
                func.count(ReviewQueueRecord.id),
            )
            .group_by(ReviewQueueRecord.status)
        )
        result = await self._session.execute(stmt)
        return {row[0]: row[1] for row in result.all()}
