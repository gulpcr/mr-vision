from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth_service import AuthService
from app.application.tenant_service import TenantService
from app.interface.api.dependencies import get_auth_service, get_session, get_tenant_service
from app.interface.middleware.auth import require_platform_admin

router = APIRouter(
    prefix="/admin/tenants", tags=["tenants"], dependencies=[Depends(require_platform_admin)],
)


class TenantResponse(BaseModel):
    id: str
    name: str
    slug: str
    is_active: bool
    status: str
    plan: str
    features: list[str]
    max_users: int | None = None
    created_at: str | None = None


class CreatedTenantResponse(TenantResponse):
    admin_username: str
    # Deprecated: provisioning no longer generates passwords (always None).
    admin_temp_password: str | None = None
    admin_invite_link: str | None = Field(
        default=None, description="One-time link for the first admin to set a password"
    )
    invite_expires_in_hours: int | None = None
    called_aet: str | None = None


class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    slug: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    plan: str = "starter"
    features: list[str] = []
    admin_username: str = Field(..., min_length=3, max_length=128)
    admin_email: EmailStr
    admin_full_name: str = ""
    # Optional DICOM called AE title for the tenant's scanners, and a seat limit.
    called_aet: str | None = Field(default=None, max_length=16)
    max_users: int | None = Field(default=None, ge=1, le=100_000)


class UpdatePlanRequest(BaseModel):
    plan: str = Field(..., min_length=1, max_length=32)


class UpdateFeaturesRequest(BaseModel):
    features: list[str]


class UpdateStatusRequest(BaseModel):
    status: str = Field(..., description="active | suspended | offboarded")


def _to_response(tenant) -> TenantResponse:
    return TenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        is_active=tenant.is_active,
        status=tenant.status,
        plan=tenant.plan,
        features=tenant.features,
        max_users=tenant.max_users,
        created_at=tenant.created_at.isoformat() if tenant.created_at else None,
    )


@router.get("", response_model=list[TenantResponse])
async def list_tenants(
    service: Annotated[TenantService, Depends(get_tenant_service)],
):
    tenants = await service.list_tenants()
    return [_to_response(t) for t in tenants]


@router.post("", response_model=CreatedTenantResponse, status_code=201)
async def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[TenantService, Depends(get_tenant_service)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
):
    """Provision a workspace: tenant + system roles (incl. Doctor) + settings + branding
    + role-default dashboards + optional DICOM AE title + seat limit + an *invited* first
    admin, all in the request's single transaction (any failure rolls everything back).
    Returns a one-time invitation link for the admin — no password is ever shown."""
    from app.application.dicom_endpoint_service import InvalidAETitleError
    from app.application.tenant_provisioning_service import TenantProvisioningService

    try:
        result = await TenantProvisioningService(session, service, auth_service).provision(
            name=body.name, slug=body.slug, plan=body.plan, features=body.features,
            admin_username=body.admin_username, admin_email=body.admin_email,
            admin_full_name=body.admin_full_name, called_aet=body.called_aet,
            max_users=body.max_users, actor=actor,
        )
    except InvalidAETitleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        status_code = 409 if "already exists" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))

    from app.interface.api.auth import _invite_link, _ttl_hours

    return CreatedTenantResponse(
        **_to_response(result.tenant).model_dump(),
        admin_username=result.admin_user.username,
        admin_temp_password=None,
        admin_invite_link=await _invite_link(request, result.invite_token, result.tenant.id),
        invite_expires_in_hours=_ttl_hours(),
        called_aet=(result.dicom_endpoint or {}).get("called_aet"),
    )


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    service: Annotated[TenantService, Depends(get_tenant_service)],
):
    tenant = await service.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return _to_response(tenant)


@router.put("/{tenant_id}/plan", response_model=TenantResponse)
async def update_tenant_plan(
    tenant_id: str,
    body: UpdatePlanRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[TenantService, Depends(get_tenant_service)],
):
    """Move a tenant to another (existing, active) plan. Its use cases and permission
    ceiling change immediately; its seat limit is left as is."""
    from app.application.plan_service import PlanNotFound, PlanService
    from app.infrastructure.auth.principal import invalidate_all_principals
    from app.infrastructure.tenant.entitlements import invalidate_entitlements

    try:
        plan = await PlanService(session).get_plan(body.plan)
    except PlanNotFound as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not plan.is_active:
        raise HTTPException(status_code=422, detail=f"Plan '{body.plan}' is inactive")
    try:
        tenant = await service.update_plan(tenant_id, body.plan)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    invalidate_entitlements()
    invalidate_all_principals()
    return _to_response(tenant)


@router.put("/{tenant_id}/features", response_model=TenantResponse)
async def update_tenant_features(
    tenant_id: str,
    body: UpdateFeaturesRequest,
    service: Annotated[TenantService, Depends(get_tenant_service)],
):
    try:
        tenant = await service.update_features(tenant_id, body.features)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    from app.infrastructure.auth.principal import invalidate_all_principals
    from app.infrastructure.tenant.entitlements import invalidate_entitlements

    invalidate_entitlements()
    invalidate_all_principals()
    return _to_response(tenant)


@router.put("/{tenant_id}/status", response_model=TenantResponse)
async def update_tenant_status(
    tenant_id: str,
    body: UpdateStatusRequest,
    service: Annotated[TenantService, Depends(get_tenant_service)],
):
    try:
        tenant = await service.set_status(tenant_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(tenant)


# ── DICOM endpoints (AE titles) ─────────────────────────────────────────────


class DicomEndpointCreateRequest(BaseModel):
    called_aet: str = Field(..., min_length=1, max_length=16)
    calling_aet: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=256)


@router.get("/{tenant_id}/dicom-endpoints")
async def list_dicom_endpoints(
    tenant_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """AE titles whose C-STORE studies are attributed to this tenant."""
    from app.application.dicom_endpoint_service import DicomEndpointService

    return await DicomEndpointService(session).list_for_tenant(tenant_id)


@router.post("/{tenant_id}/dicom-endpoints", status_code=201)
async def create_dicom_endpoint(
    tenant_id: str,
    body: DicomEndpointCreateRequest,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_service: Annotated[TenantService, Depends(get_tenant_service)],
):
    """Assign a called AE title to this tenant (optionally only from one calling AE)."""
    from app.application.dicom_endpoint_service import DicomEndpointService, InvalidAETitleError
    from app.domain.enums import AuditAction
    from app.domain.models import AuditEntry
    from app.infrastructure.database.repositories import PgAuditRepository

    if await tenant_service.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    try:
        endpoint = await DicomEndpointService(session).create(
            tenant_id, body.called_aet, body.calling_aet, body.description
        )
    except InvalidAETitleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await PgAuditRepository(session, tenant_id=tenant_id).save(AuditEntry(
        action=AuditAction.CONFIG_CHANGED,
        entity_type="dicom_endpoint",
        entity_id=endpoint["id"],
        actor=actor,
        details={"called_aet": endpoint["called_aet"], "calling_aet": endpoint["calling_aet"]},
        tenant_id=tenant_id,
    ))
    return endpoint


@router.delete("/{tenant_id}/dicom-endpoints/{endpoint_id}", status_code=204)
async def delete_dicom_endpoint(
    tenant_id: str,
    endpoint_id: str,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.dicom_endpoint_service import DicomEndpointService
    from app.domain.enums import AuditAction
    from app.domain.models import AuditEntry
    from app.infrastructure.database.repositories import PgAuditRepository

    if not await DicomEndpointService(session).delete(tenant_id, endpoint_id):
        raise HTTPException(status_code=404, detail="DICOM endpoint not found")
    await PgAuditRepository(session, tenant_id=tenant_id).save(AuditEntry(
        action=AuditAction.CONFIG_CHANGED,
        entity_type="dicom_endpoint",
        entity_id=endpoint_id,
        actor=actor,
        details={"deleted": True},
        tenant_id=tenant_id,
    ))
