from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class ModelRegistryService:
    """Manages model versions in the registry."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def register_version(
        self,
        usecase_name: str,
        version: str,
        storage_path: str,
        checksum: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import ModelVersionRecord

        record = ModelVersionRecord(
            id=str(uuid.uuid4()),
            usecase_name=usecase_name,
            version=version,
            storage_path=storage_path,
            checksum=checksum,
            is_active=False,
            metadata_=metadata or {},
        )
        self._session.add(record)
        await self._session.flush()
        return {
            "id": record.id,
            "usecase_name": usecase_name,
            "version": version,
            "storage_path": storage_path,
            "is_active": False,
        }

    async def activate_version(self, usecase_name: str, version: str) -> bool:
        """Set a specific version as active, deactivating others."""
        from app.infrastructure.database.models import ModelVersionRecord

        # Deactivate all versions for this usecase
        deactivate_stmt = (
            update(ModelVersionRecord)
            .where(ModelVersionRecord.usecase_name == usecase_name)
            .values(is_active=False)
        )
        await self._session.execute(deactivate_stmt)

        # Activate the target version
        activate_stmt = (
            update(ModelVersionRecord)
            .where(
                ModelVersionRecord.usecase_name == usecase_name,
                ModelVersionRecord.version == version,
            )
            .values(is_active=True)
        )
        result = await self._session.execute(activate_stmt)
        await self._session.flush()

        logger.info(
            "model_version_activated",
            usecase=usecase_name,
            version=version,
        )
        return result.rowcount > 0

    async def get_active_version(self, usecase_name: str) -> dict[str, Any] | None:
        from app.infrastructure.database.models import ModelVersionRecord

        stmt = select(ModelVersionRecord).where(
            ModelVersionRecord.usecase_name == usecase_name,
            ModelVersionRecord.is_active == True,
        )
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            return None
        return {
            "id": record.id,
            "usecase_name": record.usecase_name,
            "version": record.version,
            "storage_path": record.storage_path,
            "checksum": record.checksum,
            "is_active": True,
            "metadata": record.metadata_,
        }

    async def list_versions(self, usecase_name: str) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import ModelVersionRecord

        stmt = (
            select(ModelVersionRecord)
            .where(ModelVersionRecord.usecase_name == usecase_name)
            .order_by(ModelVersionRecord.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "usecase_name": r.usecase_name,
                "version": r.version,
                "storage_path": r.storage_path,
                "checksum": r.checksum,
                "is_active": r.is_active,
                "metadata": r.metadata_,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    async def delete_version(self, usecase_name: str, version: str) -> bool:
        from app.infrastructure.database.models import ModelVersionRecord
        from sqlalchemy import delete

        stmt = delete(ModelVersionRecord).where(
            ModelVersionRecord.usecase_name == usecase_name,
            ModelVersionRecord.version == version,
            ModelVersionRecord.is_active == False,
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0
