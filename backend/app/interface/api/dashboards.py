"""Dashboards API — per-tenant role defaults, personal and shared dashboards, widget data.

GET    /dashboards                          dashboard.view    dashboards visible to the caller
GET    /dashboards/widget-types             dashboard.view    widget catalogue (filtered by permission)
POST   /dashboards                          dashboard.manage  create a personal dashboard
GET    /dashboards/{id}                     dashboard.view
PUT    /dashboards/{id}                     dashboard.manage  owners; admins also role defaults/shared
DELETE /dashboards/{id}                     dashboard.manage
POST   /dashboards/{id}/clone               dashboard.manage
GET    /dashboards/{id}/versions            dashboard.view
POST   /dashboards/{id}/versions/{n}/restore dashboard.manage
POST   /dashboards/{id}/data                dashboard.view    data for every widget
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dashboard_service import (
    DashboardForbidden,
    DashboardNotFound,
    DashboardService,
    DashboardValidationError,
    widget_types_for,
)
from app.infrastructure.tenant.db_scope import current_referring_user_id
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


class DashboardPayload(BaseModel):
    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    widgets: list[dict[str, Any]] | None = None
    filters: dict[str, Any] | None = None
    is_shared: bool | None = None
    refresh_interval: int | None = Field(default=None, ge=30, le=3600)


class DataRequest(BaseModel):
    filters: dict[str, Any] | None = None


class CloneRequest(BaseModel):
    name: str | None = Field(default=None, max_length=256)


def _service(request: Request, session: AsyncSession) -> DashboardService:
    return DashboardService(
        session,
        tenant_id=getattr(request.state, "tenant_id", "default") or "default",
        user_id=getattr(request.state, "user_id", "") or "",
        role=(getattr(request.state, "roles", None) or [None])[0],
        permissions=getattr(request.state, "permissions", None) or (),
        referring_user_id=current_referring_user_id(),
        actor=getattr(request.state, "user", "unknown"),
    )


def _map(exc: Exception) -> HTTPException:
    if isinstance(exc, DashboardNotFound):
        return HTTPException(404, "Dashboard not found")
    if isinstance(exc, DashboardForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, DashboardValidationError):
        return HTTPException(422, str(exc))
    raise exc


@router.get("", dependencies=[require_permission("dashboard.view")])
async def list_dashboards(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    return await _service(request, session).list()


@router.get("/widget-types", dependencies=[require_permission("dashboard.view")])
async def widget_types(request: Request):
    return widget_types_for(getattr(request.state, "permissions", None) or ())


@router.post("", status_code=201, dependencies=[require_permission("dashboard.manage")])
async def create_dashboard(
    body: DashboardPayload, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).create(body.model_dump(exclude_none=True))
    except (DashboardForbidden, DashboardValidationError) as e:
        raise _map(e)


@router.get("/{dashboard_id}", dependencies=[require_permission("dashboard.view")])
async def get_dashboard(
    dashboard_id: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).get(dashboard_id)
    except DashboardNotFound as e:
        raise _map(e)


@router.put("/{dashboard_id}", dependencies=[require_permission("dashboard.manage")])
async def update_dashboard(
    dashboard_id: str, body: DashboardPayload, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).update(dashboard_id, body.model_dump(exclude_none=True))
    except (DashboardNotFound, DashboardForbidden, DashboardValidationError) as e:
        raise _map(e)


@router.delete("/{dashboard_id}", status_code=204, dependencies=[require_permission("dashboard.manage")])
async def delete_dashboard(
    dashboard_id: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        await _service(request, session).delete(dashboard_id)
    except (DashboardNotFound, DashboardForbidden) as e:
        raise _map(e)


@router.post("/{dashboard_id}/clone", status_code=201, dependencies=[require_permission("dashboard.manage")])
async def clone_dashboard(
    dashboard_id: str, body: CloneRequest, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).clone(dashboard_id, body.name)
    except (DashboardNotFound, DashboardForbidden, DashboardValidationError) as e:
        raise _map(e)


@router.get("/{dashboard_id}/versions", dependencies=[require_permission("dashboard.view")])
async def list_versions(
    dashboard_id: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).versions(dashboard_id)
    except DashboardNotFound as e:
        raise _map(e)


@router.post(
    "/{dashboard_id}/versions/{version}/restore",
    dependencies=[require_permission("dashboard.manage")],
)
async def restore_version(
    dashboard_id: str, version: int, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).restore(dashboard_id, version)
    except (DashboardNotFound, DashboardForbidden, DashboardValidationError) as e:
        raise _map(e)


@router.post("/{dashboard_id}/data", dependencies=[require_permission("dashboard.view")])
async def dashboard_data(
    dashboard_id: str, body: DataRequest, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).data(dashboard_id, body.filters)
    except DashboardNotFound as e:
        raise _map(e)
