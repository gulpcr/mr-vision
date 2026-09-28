"""The caller's own workspace (tenant): branding, settings, identity.

GET /tenant/public-branding  public  login-page branding for the workspace named by
                                ?workspace= / X-Tenant-Slug / subdomain
GET /tenant/current    any authenticated user   own workspace name, branding, settings
PUT /tenant/settings   settings.manage          report header/signatories, timezone
PUT /tenant/branding   settings.manage          display name, logo, colours
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.tenant_settings_service import SettingsValidationError, TenantSettingsService
from app.domain.models import AuditEntry
from app.infrastructure.database.repositories import PgAuditRepository
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/tenant", tags=["workspace"])


class SettingsUpdate(BaseModel):
    institution_name: str | None = Field(default=None, max_length=256)
    institution_address: str | None = Field(default=None, max_length=2000)
    report_header: str | None = Field(default=None, max_length=4000)
    report_footer: str | None = Field(default=None, max_length=4000)
    signatory_name: str | None = Field(default=None, max_length=256)
    signatory_title: str | None = Field(default=None, max_length=256)
    signatory_qualifications: str | None = Field(default=None, max_length=256)
    secondary_signatory_name: str | None = Field(default=None, max_length=256)
    timezone: str | None = Field(default=None, max_length=64)


class BrandingUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=256)
    logo_data_url: str | None = Field(default=None, max_length=420_000)
    primary_color: str | None = Field(default=None, max_length=7)
    accent_color: str | None = Field(default=None, max_length=7)


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


@router.get("/public-branding")
async def public_branding(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace: str | None = None,
):
    """Public (login page): branding of the named workspace, or the platform default
    when none is named. Exposes nothing beyond the name, logo and colours."""
    from app.config import get_settings
    from app.infrastructure.tenant.repository import get_tenant_by_slug
    from app.interface.middleware.tenant import _resolve_tenant_slug

    slug = (workspace or "").strip().lower() or _resolve_tenant_slug(
        request, get_settings().tenant_root_domain
    )
    if not slug:
        return {"workspace": None, "display_name": None, "logo_data_url": None,
                "primary_color": None, "accent_color": None}
    tenant = await get_tenant_by_slug(slug)
    if tenant is None or tenant.status != "active":
        raise HTTPException(status_code=404, detail="Unknown workspace")
    branding = await TenantSettingsService(session).get_branding(tenant.tenant_id)
    return {"workspace": tenant.slug, **branding}


@router.get("/current")
async def current_workspace(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    from app.infrastructure.tenant.repository import get_tenant_by_id

    tenant_id = _tenant(request)
    tenant = await get_tenant_by_id(tenant_id)
    service = TenantSettingsService(session)
    return {
        "tenant_id": tenant_id,
        "workspace": tenant.slug if tenant else tenant_id,
        "plan": tenant.plan if tenant else None,
        "features": tenant.features if tenant else [],
        "branding": await service.get_branding(tenant_id),
        "settings": await service.get_settings(tenant_id),
    }


async def _audit(session, request: Request, what: str, fields: list[str]) -> None:
    await PgAuditRepository(session, tenant_id=_tenant(request)).save(AuditEntry(
        action="tenant_settings_updated", entity_type="tenant", entity_id=_tenant(request),
        actor=getattr(request.state, "user", "unknown"),
        details={"section": what, "fields": fields}, tenant_id=_tenant(request),
    ))


@router.put("/settings", dependencies=[require_permission("settings.manage")])
async def update_settings(
    body: SettingsUpdate, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    payload: dict[str, Any] = body.model_dump(exclude_unset=True)
    result = await TenantSettingsService(session).update_settings(_tenant(request), payload)
    await _audit(session, request, "settings", sorted(payload))
    return result


@router.put("/branding", dependencies=[require_permission("settings.manage")])
async def update_branding(
    body: BrandingUpdate, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    payload: dict[str, Any] = body.model_dump(exclude_unset=True)
    try:
        result = await TenantSettingsService(session).update_branding(_tenant(request), payload)
    except SettingsValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _audit(session, request, "branding", sorted(payload))
    return result
