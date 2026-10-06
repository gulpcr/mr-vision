from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import client_ip_from
from app.application.break_glass_service import BreakGlassError, BreakGlassService
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/break-glass", tags=["break-glass"])


class BreakGlassRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64, description="The patient's MRN")
    reason: str = Field(min_length=1, max_length=4000)


def _service(request: Request, session: AsyncSession) -> BreakGlassService:
    return BreakGlassService(session, getattr(request.state, "tenant_id", "default") or "default")


def _forget_viewer_cache() -> None:
    """A new/ended grant changes which studies the viewer may open right now."""
    from app.interface.api import viewer_access

    viewer_access._ownership_cache.clear()


@router.post("", status_code=201, dependencies=[require_permission("break_glass.invoke")])
async def invoke_break_glass(
    body: BreakGlassRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Emergency access to one patient outside your referrals: time-limited, the reason
    is mandatory, and it is audited and reviewed by your workspace administrators."""
    if getattr(request.state, "impersonated_by", None):
        raise HTTPException(status_code=403, detail="Emergency access cannot be used while impersonating")
    try:
        grant = await _service(request, session).invoke(
            user_id=request.state.user_id, username=getattr(request.state, "user", ""),
            patient_id=body.patient_id, reason=body.reason, client_ip=client_ip_from(request),
        )
    except BreakGlassError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await session.commit()
    _forget_viewer_cache()
    return grant


@router.get("/mine", dependencies=[require_permission("break_glass.invoke")])
async def my_break_glass_grants(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    return {"grants": await _service(request, session).mine(request.state.user_id)}


@router.get("", dependencies=[require_permission("break_glass.review")])
async def list_break_glass_grants(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    """All emergency-access grants in the workspace, newest first (for review)."""
    return {"grants": await _service(request, session).list()}


@router.post("/{grant_id}/revoke", dependencies=[require_permission("break_glass.review")])
async def revoke_break_glass_grant(
    grant_id: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    grant = await _service(request, session).revoke(grant_id, getattr(request.state, "user", "unknown"))
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    await session.commit()
    _forget_viewer_cache()
    return grant
