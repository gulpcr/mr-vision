from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.tenant_api_key_service import TenantApiKeyService
from app.domain.enums import AuditAction
from app.domain.models import AuditEntry
from app.infrastructure.database.repositories import PgAuditRepository
from app.interface.api.dependencies import get_session, get_tenant_api_key_service
from app.interface.middleware.auth import require_platform_admin

router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/api-keys",
    tags=["tenant-api-keys"],
    dependencies=[Depends(require_platform_admin)],
)

VALID_SCOPES = frozenset({"dicom:upload"})


class TenantApiKeyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    prefix: str
    scopes: list[str]
    expires_at: str | None = None
    is_active: bool
    last_used_at: str | None = None
    revoked_at: str | None = None
    created_at: str | None = None


class CreatedTenantApiKeyResponse(TenantApiKeyResponse):
    key: str = Field(..., description="Full cleartext key — shown once, never retrievable again")


class CreateTenantApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    scopes: list[str] = Field(..., min_length=1)
    expires_at: str | None = None


def _to_response(key) -> TenantApiKeyResponse:
    return TenantApiKeyResponse(
        id=key.id,
        tenant_id=key.tenant_id,
        name=key.name,
        prefix=key.prefix,
        scopes=key.scopes,
        expires_at=key.expires_at.isoformat() if key.expires_at else None,
        is_active=key.is_active,
        last_used_at=key.last_used_at.isoformat() if key.last_used_at else None,
        revoked_at=key.revoked_at.isoformat() if key.revoked_at else None,
        created_at=key.created_at.isoformat() if key.created_at else None,
    )


@router.get("", response_model=list[TenantApiKeyResponse])
async def list_tenant_api_keys(
    tenant_id: str,
    service: Annotated[TenantApiKeyService, Depends(get_tenant_api_key_service)],
):
    keys = await service.list_keys(tenant_id)
    return [_to_response(k) for k in keys]


@router.post("", response_model=CreatedTenantApiKeyResponse, status_code=201)
async def create_tenant_api_key(
    tenant_id: str,
    body: CreateTenantApiKeyRequest,
    request: Request,
    service: Annotated[TenantApiKeyService, Depends(get_tenant_api_key_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    unknown_scopes = set(body.scopes) - VALID_SCOPES
    if unknown_scopes:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope(s): {sorted(unknown_scopes)}. Valid scopes: {sorted(VALID_SCOPES)}",
        )

    expires_at = None
    if body.expires_at:
        from datetime import datetime
        try:
            expires_at = datetime.fromisoformat(body.expires_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="expires_at must be an ISO-8601 datetime")

    key, plaintext = await service.create_key(
        tenant_id=tenant_id, name=body.name, scopes=body.scopes, expires_at=expires_at,
    )
    await PgAuditRepository(session).save(AuditEntry(
        action=AuditAction.API_KEY_CREATED,
        entity_type="tenant_api_key",
        entity_id=key.id,
        actor=getattr(request.state, "user", "unknown"),
        details={"name": key.name, "scopes": key.scopes},
        tenant_id=tenant_id,
    ))
    return CreatedTenantApiKeyResponse(**_to_response(key).model_dump(), key=plaintext)


@router.delete("/{key_id}", response_model=TenantApiKeyResponse)
async def revoke_tenant_api_key(
    tenant_id: str,
    key_id: str,
    request: Request,
    service: Annotated[TenantApiKeyService, Depends(get_tenant_api_key_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        key = await service.revoke_key(tenant_id, key_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await PgAuditRepository(session).save(AuditEntry(
        action=AuditAction.API_KEY_REVOKED,
        entity_type="tenant_api_key",
        entity_id=key.id,
        actor=getattr(request.state, "user", "unknown"),
        details={"name": key.name},
        tenant_id=tenant_id,
    ))
    return _to_response(key)
