"""Referring Physician Portal Service.

Creates time-limited, scoped share links for read-only result access.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_DEFAULT_TTL_DAYS = 7


class PortalService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_share_link(
        self,
        result_id: str,
        study_instance_uid: str,
        usecase_name: str,
        created_by: str = "system",
        ttl_days: int = _DEFAULT_TTL_DAYS,
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import ShareLinkRecord

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

        record = ShareLinkRecord(
            id=str(uuid.uuid4()),
            result_id=result_id,
            study_instance_uid=study_instance_uid,
            usecase_name=usecase_name,
            token=token,
            created_by=created_by,
            expires_at=expires_at,
            is_active=True,
        )
        self._session.add(record)
        await self._session.flush()

        logger.info("share_link_created", result_id=result_id, created_by=created_by)
        return {
            "id": record.id,
            "token": token,
            "result_id": result_id,
            "study_instance_uid": study_instance_uid,
            "usecase_name": usecase_name,
            "expires_at": expires_at.isoformat(),
            "created_by": created_by,
        }

    async def resolve_token(self, token: str) -> dict[str, Any] | None:
        """Return share link metadata if the token is valid and not expired."""
        from app.infrastructure.database.models import ShareLinkRecord

        stmt = select(ShareLinkRecord).where(
            ShareLinkRecord.token == token,
            ShareLinkRecord.is_active == True,
        )
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return None

        now = datetime.now(timezone.utc)
        exp = record.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now > exp:
            return None

        return {
            "id": record.id,
            "result_id": record.result_id,
            "study_instance_uid": record.study_instance_uid,
            "usecase_name": record.usecase_name,
            "expires_at": record.expires_at.isoformat(),
        }

    async def revoke_link(self, link_id: str) -> bool:
        from app.infrastructure.database.models import ShareLinkRecord

        stmt = select(ShareLinkRecord).where(ShareLinkRecord.id == link_id)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            return False
        record.is_active = False
        await self._session.flush()
        return True

    async def list_links_for_result(self, result_id: str) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import ShareLinkRecord

        stmt = (
            select(ShareLinkRecord)
            .where(ShareLinkRecord.result_id == result_id)
            .order_by(ShareLinkRecord.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "token": r.token[:8] + "...",  # truncate for security in list
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "is_active": r.is_active,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]
