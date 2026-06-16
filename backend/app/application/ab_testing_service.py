from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class ABTestingService:
    """Manages A/B testing experiments for model versions."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_experiment(
        self,
        name: str,
        usecase_name: str,
        control_version: str,
        treatment_version: str,
        traffic_split: float = 0.5,
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import ABExperimentRecord

        exp_id = str(uuid.uuid4())
        record = ABExperimentRecord(
            id=exp_id,
            name=name,
            usecase_name=usecase_name,
            control_version=control_version,
            treatment_version=treatment_version,
            traffic_split=traffic_split,
            is_active=True,
        )
        self._session.add(record)
        await self._session.flush()
        return {
            "id": exp_id,
            "name": name,
            "usecase_name": usecase_name,
            "control_version": control_version,
            "treatment_version": treatment_version,
            "traffic_split": traffic_split,
            "is_active": True,
        }

    async def assign_version(
        self, experiment_id: str, study_instance_uid: str
    ) -> str:
        """Assign a model version for a study in an experiment."""
        from app.infrastructure.database.models import (
            ABAssignmentRecord,
            ABExperimentRecord,
        )

        # Check for existing assignment
        stmt = select(ABAssignmentRecord).where(
            ABAssignmentRecord.experiment_id == experiment_id,
            ABAssignmentRecord.study_instance_uid == study_instance_uid,
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            return existing.assigned_version

        # Get experiment
        exp_stmt = select(ABExperimentRecord).where(
            ABExperimentRecord.id == experiment_id
        )
        exp_result = await self._session.execute(exp_stmt)
        experiment = exp_result.scalar_one_or_none()
        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        # Random assignment based on traffic split
        version = (
            experiment.control_version
            if random.random() >= experiment.traffic_split
            else experiment.treatment_version
        )

        assignment = ABAssignmentRecord(
            id=str(uuid.uuid4()),
            experiment_id=experiment_id,
            study_instance_uid=study_instance_uid,
            assigned_version=version,
        )
        self._session.add(assignment)
        await self._session.flush()
        return version

    async def list_experiments(
        self, usecase_name: str | None = None
    ) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import ABExperimentRecord

        stmt = select(ABExperimentRecord).order_by(
            ABExperimentRecord.created_at.desc()
        )
        if usecase_name:
            stmt = stmt.where(ABExperimentRecord.usecase_name == usecase_name)
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "name": r.name,
                "usecase_name": r.usecase_name,
                "control_version": r.control_version,
                "treatment_version": r.treatment_version,
                "traffic_split": r.traffic_split,
                "is_active": r.is_active,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    async def get_experiment_stats(self, experiment_id: str) -> dict[str, Any]:
        from app.infrastructure.database.models import ABAssignmentRecord
        from sqlalchemy import func

        stmt = (
            select(
                ABAssignmentRecord.assigned_version,
                func.count(ABAssignmentRecord.id),
            )
            .where(ABAssignmentRecord.experiment_id == experiment_id)
            .group_by(ABAssignmentRecord.assigned_version)
        )
        result = await self._session.execute(stmt)
        stats = {row[0]: row[1] for row in result.all()}
        return {"experiment_id": experiment_id, "assignments": stats}

    async def stop_experiment(self, experiment_id: str) -> bool:
        from app.infrastructure.database.models import ABExperimentRecord

        stmt = (
            update(ABExperimentRecord)
            .where(ABExperimentRecord.id == experiment_id)
            .values(is_active=False)
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return True
