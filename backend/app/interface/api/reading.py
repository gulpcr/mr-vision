"""Reading-workflow API — study lifecycle, assignment, and turnaround.

POST /studies/{uid}/claim        require(study.claim)    self-claim → in_progress
POST /studies/{uid}/assign       require(study.claim)    assign/reassign to a radiologist
POST /studies/{uid}/auto-assign  require(study.claim)    load-balance to least-loaded radiologist
POST /studies/{uid}/unclaim      require(study.claim)    release → unread (assignee/admin)
POST /studies/{uid}/report       require(result.approve) in_progress → reported (assignee/admin)
POST /studies/{uid}/sign         require(result.approve) reported → signed (assignee/admin)
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth_service import AuthService
from app.application.reading_service import (
    InvalidTransitionError,
    NoRadiologistError,
    NotAssignedError,
    ReadingService,
    StudyNotFoundError,
)
from app.interface.api.dependencies import get_session
from app.interface.api.validators import validate_dicom_uid
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/studies", tags=["reading"])


def _actor(request: Request) -> tuple[str, str, bool, str]:
    """(user_id, username, is_admin, tenant_id) from the authenticated request."""
    roles = getattr(request.state, "roles", []) or []
    return (
        getattr(request.state, "user_id", "") or "",
        getattr(request.state, "user", "system") or "system",
        ("admin" in roles or "system" in roles),
        getattr(request.state, "tenant_id", "default") or "default",
    )


def _map_errors(exc: Exception):
    if isinstance(exc, StudyNotFoundError):
        return HTTPException(404, "Study not found")
    if isinstance(exc, NotAssignedError):
        return HTTPException(403, str(exc))
    if isinstance(exc, (InvalidTransitionError, NoRadiologistError)):
        return HTTPException(409, str(exc))
    return None


@router.post("/{study_uid}/claim", dependencies=[require_permission("study.claim")])
async def claim_study(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    uid, username, _is_admin, _tenant = _actor(request)
    try:
        result = await ReadingService(session).claim(study_uid, uid, username)
    except Exception as exc:
        mapped = _map_errors(exc)
        if mapped:
            raise mapped
        raise
    await session.commit()
    return result


@router.post("/{study_uid}/assign", dependencies=[require_permission("study.claim")])
async def assign_study(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    assignee_id: str = Query(..., description="User id of the radiologist to assign"),
):
    validate_dicom_uid(study_uid)
    _uid, actor, _is_admin, _tenant = _actor(request)
    assignee = await AuthService(session).get_user_by_id(assignee_id)
    if not assignee or not assignee.is_active:
        raise HTTPException(404, "Assignee not found or inactive")
    if assignee.role != "radiologist":
        raise HTTPException(422, "Studies can only be assigned to a radiologist")
    try:
        result = await ReadingService(session).assign(
            study_uid, assignee.id, assignee.username, actor=actor
        )
    except Exception as exc:
        mapped = _map_errors(exc)
        if mapped:
            raise mapped
        raise
    await session.commit()
    return result


@router.post("/{study_uid}/auto-assign", dependencies=[require_permission("study.claim")])
async def auto_assign_study(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    _uid, actor, _is_admin, tenant = _actor(request)
    try:
        result = await ReadingService(session).auto_assign(study_uid, actor=actor, tenant_id=tenant)
    except Exception as exc:
        mapped = _map_errors(exc)
        if mapped:
            raise mapped
        raise
    await session.commit()
    return result


@router.post("/{study_uid}/unclaim", dependencies=[require_permission("study.claim")])
async def unclaim_study(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    uid, actor, is_admin, _tenant = _actor(request)
    try:
        result = await ReadingService(session).unclaim(study_uid, uid, actor, is_admin)
    except Exception as exc:
        mapped = _map_errors(exc)
        if mapped:
            raise mapped
        raise
    await session.commit()
    return result


@router.post("/{study_uid}/report", dependencies=[require_permission("result.approve")])
async def report_study(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    uid, actor, is_admin, _tenant = _actor(request)
    try:
        result = await ReadingService(session).report(study_uid, uid, actor, is_admin)
    except Exception as exc:
        mapped = _map_errors(exc)
        if mapped:
            raise mapped
        raise
    await session.commit()
    return result


@router.post("/{study_uid}/sign", dependencies=[require_permission("result.approve")])
async def sign_study(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    uid, actor, is_admin, _tenant = _actor(request)
    try:
        result = await ReadingService(session).sign(study_uid, uid, actor, is_admin)
    except Exception as exc:
        mapped = _map_errors(exc)
        if mapped:
            raise mapped
        raise
    await session.commit()
    return result
