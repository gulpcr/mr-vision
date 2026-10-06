from __future__ import annotations

"""Audit-log review periods (alembic 055): list, read, generate on demand, mark reviewed."""

import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_review_service import (
    AuditReviewNotFound,
    AuditReviewService,
    previous_month,
)
from app.application.audit_service import client_ip_from
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/admin/audit-reviews", tags=["audit-review"])

_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class GenerateRequest(BaseModel):
    period: str | None = Field(None, description="YYYY-MM; default: the previous month")


class ReviewRequest(BaseModel):
    notes: str = Field("", max_length=8000)


def _service(request: Request, session: AsyncSession) -> AuditReviewService:
    return AuditReviewService(session, getattr(request.state, "tenant_id", "default") or "default")


def _month(period: str) -> tuple[datetime, datetime]:
    m = _PERIOD.match(period)
    if not m:
        raise HTTPException(status_code=422, detail="period must be YYYY-MM")
    year, month = int(m.group(1)), int(m.group(2))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    if start >= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="period has not started yet")
    return start, end


@router.get("", dependencies=[require_permission("audit.view")])
async def list_audit_reviews(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    return {"reviews": await _service(request, session).list()}


@router.post("", status_code=201, dependencies=[require_permission("audit.view")])
async def generate_audit_review(
    body: GenerateRequest, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    """Build (or return the existing) review for a month. A month still in progress is
    summarised up to now and should be regenerated after it ends."""
    start, end = _month(body.period) if body.period else previous_month()
    review = await _service(request, session).generate(
        start, min(end, datetime.now(timezone.utc)), actor=getattr(request.state, "user", "") or "unknown",
    )
    await session.commit()
    return review


@router.get("/{review_id}", dependencies=[require_permission("audit.view")])
async def get_audit_review(
    review_id: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        return await _service(request, session).get(review_id)
    except AuditReviewNotFound:
        raise HTTPException(status_code=404, detail="Review not found")


@router.post("/{review_id}/review", dependencies=[require_permission("audit.view")])
async def mark_audit_review_reviewed(
    review_id: str, body: ReviewRequest, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Attest that this period's activity was reviewed (who, when, notes; audited)."""
    if getattr(request.state, "impersonated_by", None):
        raise HTTPException(status_code=403, detail="Reviews cannot be signed off while impersonating")
    try:
        review = await _service(request, session).mark_reviewed(
            review_id, getattr(request.state, "user_id", "") or "",
            getattr(request.state, "user", "") or "unknown", body.notes, client_ip_from(request),
        )
    except AuditReviewNotFound:
        raise HTTPException(status_code=404, detail="Review not found")
    await session.commit()
    return review
