from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.application.auth_service import AuthService
from app.application.tenant_service import TenantService
from app.interface.api.dependencies import get_auth_service, get_tenant_service
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
    created_at: str | None = None


class CreatedTenantResponse(TenantResponse):
    admin_username: str
    admin_temp_password: str = Field(
        ..., description="Shown once — the admin should change it on first login"
    )


class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    slug: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    plan: str = "starter"
    features: list[str] = []
    admin_username: str = Field(..., min_length=3, max_length=128)
    admin_email: EmailStr
    admin_full_name: str = ""


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
    service: Annotated[TenantService, Depends(get_tenant_service)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
):
    """Creates the tenant and its first admin user together.

    Both go through the same request-scoped session (get_tenant_service and
    get_auth_service both resolve to it via Depends(get_session)), so if
    creating the admin user fails after the tenant row was already flushed,
    the whole session rolls back at the request boundary — the tenant is never
    left behind half-provisioned with no way to log in.
    """
    try:
        tenant = await service.create_tenant(
            name=body.name, slug=body.slug, plan=body.plan, features=body.features,
        )
    except ValueError as e:
        status_code = 409 if "already exists" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))

    temp_password = secrets.token_urlsafe(12)
    try:
        admin_user = await auth_service.create_user(
            username=body.admin_username,
            email=body.admin_email,
            password=temp_password,
            full_name=body.admin_full_name,
            role="admin",
            tenant_id=tenant.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return CreatedTenantResponse(
        **_to_response(tenant).model_dump(),
        admin_username=admin_user.username,
        admin_temp_password=temp_password,
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
    service: Annotated[TenantService, Depends(get_tenant_service)],
):
    try:
        tenant = await service.update_plan(tenant_id, body.plan)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
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
