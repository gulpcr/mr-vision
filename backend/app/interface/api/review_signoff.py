"""Priority review queue, electronic report signatures and report comments.

GET  /review-queue                  require(study.view | study.view.referred)
       ?status=unsigned|signed&priority=critical|abnormal|normal
PUT  /studies/{uid}/priority        require(result.approve)   manual priority override (null clears)
GET  /studies/{uid}/signoff         require(study.view | study.view.referred)
       priority + reasons, signatures, integrity, attestation statement, comments
POST /studies/{uid}/signature       require(result.approve)   electronic signature
GET  /studies/{uid}/comments        require(study.view | study.view.referred)
POST /studies/{uid}/comments        require(report.comment)
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.review_signoff_service import (
    ReviewSignoffService,
    SignoffConflictError,
    SignoffForbiddenError,
    SignoffValidationError,
    StudyNotFoundError,
)
from app.domain.permissions import STUDY_READ
from app.infrastructure.database.repositories import PgAuditRepository
from app.infrastructure.tenant.db_scope import current_referring_user_id
from app.interface.api.dependencies import get_session
from app.interface.api.validators import validate_dicom_uid
from app.interface.middleware.auth import require_permission

router = APIRouter(tags=["review-signoff"])


class PriorityUpdate(BaseModel):
    priority: Literal["critical", "abnormal", "normal"] | None = None


class SignatureRequest(BaseModel):
    statement_version: str = Field(..., max_length=32)
    agreed: bool
    comment: str = Field(..., max_length=4000)
    # Only used (and then required) when the signer's profile has no full name yet.
    full_name: str | None = Field(default=None, max_length=256)


class CommentRequest(BaseModel):
    body: str = Field(..., max_length=4000)


def _service(request: Request, session: AsyncSession) -> ReviewSignoffService:
    tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    return ReviewSignoffService(
        session,
        tenant_id=tenant_id,
        user_id=getattr(request.state, "user_id", "") or "",
        username=getattr(request.state, "user", "unknown") or "unknown",
        referring_user_id=current_referring_user_id(),
        audit_repo=PgAuditRepository(session, tenant_id=tenant_id),
    )


def _is_admin(request: Request) -> bool:
    roles = getattr(request.state, "roles", []) or []
    return "admin" in roles or "system" in roles


def _client_ip(request: Request) -> str | None:
    # nginx overwrites X-Real-IP with the peer address, so it cannot be client-supplied.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


def _mapped(exc: Exception) -> HTTPException | None:
    if isinstance(exc, StudyNotFoundError):
        return HTTPException(404, "Study not found")
    if isinstance(exc, SignoffForbiddenError):
        return HTTPException(403, str(exc))
    if isinstance(exc, SignoffConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, SignoffValidationError):
        return HTTPException(422, str(exc))
    return None


async def _run(coro, session: AsyncSession, commit: bool = False):
    try:
        result = await coro
    except Exception as exc:
        mapped = _mapped(exc)
        if mapped:
            raise mapped
        raise
    if commit:
        await session.commit()
    return result


@router.get("/review-queue", dependencies=[require_permission(STUDY_READ)])
async def review_queue(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Literal["unsigned", "signed"] = Query("unsigned"),
    priority: Literal["critical", "abnormal", "normal"] | None = Query(None),
):
    return await _run(_service(request, session).queue(status=status, priority=priority), session)


@router.put("/studies/{study_uid}/priority", dependencies=[require_permission("result.approve")])
async def set_priority(
    study_uid: str,
    body: PriorityUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    return await _run(_service(request, session).set_priority(study_uid, body.priority), session, commit=True)


@router.get("/studies/{study_uid}/signoff", dependencies=[require_permission(STUDY_READ)])
async def get_signoff(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    return await _run(_service(request, session).get_signoff(study_uid), session)


@router.post("/studies/{study_uid}/signature", dependencies=[require_permission("result.approve")])
async def sign_report(
    study_uid: str,
    body: SignatureRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    coro = _service(request, session).sign(
        study_uid,
        statement_version=body.statement_version,
        agreed=body.agreed,
        comment=body.comment,
        full_name=body.full_name,
        is_admin=_is_admin(request),
        client_ip=_client_ip(request),
    )
    return await _run(coro, session, commit=True)


@router.get("/studies/{study_uid}/comments", dependencies=[require_permission(STUDY_READ)])
async def list_comments(
    study_uid: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    return await _run(_service(request, session).list_comments(study_uid), session)


@router.post("/studies/{study_uid}/comments", status_code=201,
             dependencies=[require_permission("report.comment")])
async def add_comment(
    study_uid: str,
    body: CommentRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    validate_dicom_uid(study_uid)
    return await _run(_service(request, session).add_comment(study_uid, body.body), session, commit=True)
