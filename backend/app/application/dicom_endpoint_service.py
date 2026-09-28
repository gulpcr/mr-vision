"""Tenant DICOM endpoints: which called AE title (optionally + calling AE) belongs to
which tenant, so C-STORE studies pushed into the shared PACS can be attributed."""
from __future__ import annotations

import re
import uuid
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import TenantDicomEndpointRecord

logger = structlog.get_logger(__name__)

# DICOM AE titles: up to 16 chars of the default character repertoire, no backslash or
# control characters; leading/trailing spaces are not significant.
_AET_RE = re.compile(r"^[A-Za-z0-9 _\-\.]{1,16}$")


class InvalidAETitleError(ValueError):
    pass


def normalize_aet(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _AET_RE.match(value):
        raise InvalidAETitleError(f"Invalid DICOM AE title: {value!r}")
    return value.upper()


class DicomEndpointService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def resolve_tenant(self, called_aet: str | None, calling_aet: str | None) -> str | None:
        """Tenant owning ``called_aet``. A row pinned to this ``calling_aet`` wins over a
        row accepting any scanner. Requires a platform DB scope (cross-tenant lookup)."""
        try:
            called = normalize_aet(called_aet)
            calling = normalize_aet(calling_aet)
        except InvalidAETitleError:
            return None
        if not called:
            return None
        rows = (
            await self._session.execute(
                select(TenantDicomEndpointRecord).where(
                    func.upper(TenantDicomEndpointRecord.called_aet) == called,
                    TenantDicomEndpointRecord.is_active == True,  # noqa: E712
                )
            )
        ).scalars().all()
        pinned = [r for r in rows if r.calling_aet and r.calling_aet.upper() == (calling or "")]
        if pinned:
            return pinned[0].tenant_id
        open_rows = [r for r in rows if not r.calling_aet]
        return open_rows[0].tenant_id if open_rows else None

    async def list_for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = (
            await self._session.execute(
                select(TenantDicomEndpointRecord)
                .where(TenantDicomEndpointRecord.tenant_id == tenant_id)
                .order_by(TenantDicomEndpointRecord.created_at)
            )
        ).scalars().all()
        return [self._to_dict(r) for r in rows]

    async def create(
        self, tenant_id: str, called_aet: str, calling_aet: str | None, description: str | None
    ) -> dict[str, Any]:
        called = normalize_aet(called_aet)
        if not called:
            raise InvalidAETitleError("called_aet is required")
        calling = normalize_aet(calling_aet)
        existing = (
            await self._session.execute(
                select(TenantDicomEndpointRecord).where(
                    func.upper(TenantDicomEndpointRecord.called_aet) == called
                )
            )
        ).scalars().all()
        # A called AE title belongs to exactly one tenant — otherwise which tenant a
        # study sent to it belongs to would be ambiguous.
        if any(r.tenant_id != tenant_id for r in existing):
            raise InvalidAETitleError(f"AE title {called} is already assigned to another workspace")
        if any((r.calling_aet or None) == calling for r in existing):
            raise InvalidAETitleError(f"AE title {called} is already configured for this workspace")
        record = TenantDicomEndpointRecord(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            called_aet=called,
            calling_aet=calling,
            description=description,
            is_active=True,
        )
        self._session.add(record)
        await self._session.flush()
        logger.info("dicom_endpoint_created", tenant_id=tenant_id, called_aet=called)
        return self._to_dict(record)

    async def delete(self, tenant_id: str, endpoint_id: str) -> bool:
        result = await self._session.execute(
            delete(TenantDicomEndpointRecord).where(
                TenantDicomEndpointRecord.id == endpoint_id,
                TenantDicomEndpointRecord.tenant_id == tenant_id,
            )
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0

    @staticmethod
    def _to_dict(r: TenantDicomEndpointRecord) -> dict[str, Any]:
        return {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "called_aet": r.called_aet,
            "calling_aet": r.calling_aet,
            "description": r.description,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
