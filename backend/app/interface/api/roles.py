"""Roles management API (RBAC).

GET    /roles                      require(study.view)   list tenant roles
GET    /roles/permissions          require(study.view)   permission catalog
POST   /roles      {name,perms}    require(user.manage)  create custom role
PATCH  /roles/{id} {name?,perms?}  require(user.manage)  edit role
DELETE /roles/{id}                 require(user.manage)  delete custom, unused role
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.role_service import (
    RoleInUseError,
    RoleProtectedError,
    RoleService,
    UnknownPermissionError,
)
from app.domain.permissions import PERMISSIONS
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/roles", tags=["roles"])


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    permissions: list[str] | None = None


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


def _actor(request: Request) -> str:
    return getattr(request.state, "user", "system") or "system"


@router.get("/permissions", dependencies=[require_permission("study.view")])
async def list_permissions():
    """Return the permission catalog (key → description) for the roles editor."""
    return {"permissions": [{"key": k, "description": v} for k, v in PERMISSIONS.items()]}


@router.get("", dependencies=[require_permission("study.view")])
async def list_roles(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    return await RoleService(session).list_roles(_tenant(request))


@router.post("", status_code=201, dependencies=[require_permission("user.manage")])
async def create_role(
    body: RoleCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        role = await RoleService(session).create_role(
            name=body.name,
            permissions=body.permissions,
            tenant_id=_tenant(request),
            actor=_actor(request),
        )
    except UnknownPermissionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return role


@router.patch("/{role_id}", dependencies=[require_permission("user.manage")])
async def update_role(
    role_id: str,
    body: RoleUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        role = await RoleService(session).update_role(
            role_id=role_id,
            tenant_id=_tenant(request),
            name=body.name,
            permissions=body.permissions,
            actor=_actor(request),
        )
    except UnknownPermissionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    await session.commit()
    return role


@router.delete("/{role_id}", status_code=204, dependencies=[require_permission("user.manage")])
async def delete_role(
    role_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        deleted = await RoleService(session).delete_role(
            role_id=role_id, tenant_id=_tenant(request), actor=_actor(request)
        )
    except RoleProtectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RoleInUseError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Role not found")
    await session.commit()
