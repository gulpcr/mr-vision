"""Subscription plans — platform admins only.

GET    /admin/plans            all plans (with how many tenants are on each)
GET    /admin/plans/catalog    use cases and permissions a plan can grant
POST   /admin/plans            create a plan
PUT    /admin/plans/{name}     edit display name, description, seats, use cases, permissions
DELETE /admin/plans/{name}     delete an unused plan
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.plan_service import PlanError, PlanNotFound, PlanService
from app.application.usecase_registry import UseCaseRegistry
from app.domain.models import AuditEntry
from app.infrastructure.database.repositories import PgAuditRepository
from app.interface.api.dependencies import get_registry, get_session
from app.interface.middleware.auth import require_platform_admin

router = APIRouter(
    prefix="/admin/plans", tags=["plans"], dependencies=[Depends(require_platform_admin)],
)


class PlanCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=32)
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    default_max_users: int | None = Field(default=None, ge=1, le=100_000)
    # None = every registered use case.
    usecases: list[str] | None = None
    permissions: list[str] = Field(default_factory=lambda: ["*"])
    is_active: bool = True


class PlanUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    default_max_users: int | None = Field(default=None, ge=1, le=100_000)
    usecases: list[str] | None = None
    permissions: list[str] | None = None
    is_active: bool | None = None


def _service(session: AsyncSession, registry: UseCaseRegistry) -> PlanService:
    return PlanService(session, registered_usecases=list(registry.usecases.keys()))


def _invalidate() -> None:
    """Plan changes alter every affected tenant's use cases and permission ceiling."""
    from app.infrastructure.auth.principal import invalidate_all_principals
    from app.infrastructure.tenant.entitlements import invalidate_entitlements

    invalidate_entitlements()
    invalidate_all_principals()


async def _audit(session, actor: str, action: str, name: str, details: dict) -> None:
    await PgAuditRepository(session, tenant_id="default").save(AuditEntry(
        action=action, entity_type="plan", entity_id=name, actor=f"platform:{actor}",
        details=details, tenant_id="default",
    ))


@router.get("")
async def list_plans(
    session: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    return await _service(session, registry).list_plans()


@router.get("/catalog")
async def plan_catalog(
    session: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    return _service(session, registry).catalog()


@router.post("", status_code=201)
async def create_plan(
    body: PlanCreate,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    try:
        plan = await _service(session, registry).create_plan(body.model_dump())
    except PlanError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await _audit(session, actor, "config_changed", plan["name"], {"plan_created": plan})
    return plan


@router.put("/{name}")
async def update_plan(
    name: str,
    body: PlanUpdate,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    # Only fields actually sent are changed; "usecases": null explicitly means "all".
    payload = body.model_dump(exclude_unset=True)
    try:
        plan = await _service(session, registry).update_plan(name, payload)
    except PlanNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PlanError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _audit(session, actor, "config_changed", name, {"plan_updated": payload})
    _invalidate()
    return plan


@router.delete("/{name}", status_code=204)
async def delete_plan(
    name: str,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    try:
        await _service(session, registry).delete_plan(name)
    except PlanNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PlanError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await _audit(session, actor, "config_changed", name, {"plan_deleted": True})
