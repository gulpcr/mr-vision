"""Bilateral mammography report API (radiologist-authored, keyed by study).

GET  /studies/{study_uid}/mammography-report       require(study.view)     {} if none
PUT  /studies/{study_uid}/mammography-report {...}  require(result.approve) upsert
GET  /studies/{study_uid}/mammography-report.pdf    require(result.export)  rendered PDF
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.mammography_service import (
    MammographyService,
    ReportValidationError,
    StudyNotFoundError,
)
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(tags=["mammography"])


class MammographyReportUpdate(BaseModel):
    laterality: str | None = None
    file_no: str | None = None
    status: str | None = None
    contact: str | None = None
    procedure: str | None = None
    clinical_features: str | None = None
    right_breast_findings: str | None = None
    left_breast_findings: str | None = None
    opinion: str | None = None
    birads_right: str | None = Field(None, description="0-6")
    birads_left: str | None = Field(None, description="0-6")
    reviewing_doctor: str | None = None
    reporting_doctor: str | None = None


def _actor_id(request: Request) -> str:
    return getattr(request.state, "user_id", "") or ""


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


@router.get(
    "/studies/{study_uid}/mammography-report",
    dependencies=[require_permission("study.view")],
)
async def get_report(
    study_uid: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Saved mammography report for a study; {} if none saved yet."""
    report = await MammographyService(session).get_report(study_uid)
    return report or {}


@router.put(
    "/studies/{study_uid}/mammography-report",
    dependencies=[require_permission("result.approve")],
)
async def upsert_report(
    study_uid: str,
    body: MammographyReportUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        report = await MammographyService(session).upsert_report(
            study_uid,
            body.model_dump(exclude_unset=True),
            actor_id=_actor_id(request),
            tenant_id=_tenant(request),
        )
        await session.commit()
        return report
    except StudyNotFoundError:
        raise HTTPException(404, f"Study {study_uid} not found")
    except ReportValidationError as e:
        raise HTTPException(422, str(e))


@router.get(
    "/studies/{study_uid}/mammography-report.pdf",
    dependencies=[require_permission("result.export")],
)
async def download_report_pdf(
    study_uid: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Render the saved mammography report to PDF."""
    from app.infrastructure.database.models import StudyRecord
    from app.reports.pdf_generator import PDFReportGenerator, build_petct_patient_info

    report = await MammographyService(session).get_report(study_uid)
    if report is None:
        raise HTTPException(404, "No mammography report saved for this study")

    study_rec = (
        await session.execute(
            select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
        )
    ).scalar_one_or_none()

    patient_info = build_petct_patient_info(study_rec)
    pdf_bytes = PDFReportGenerator().generate(
        study_uid=study_uid,
        usecase_name="mammography",
        result={"summary": report, "measurements": {}, "qa_flags": [], "qa_details": {}},
        patient_info=patient_info,
        narrative="",
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="mammography_{study_uid[:8]}.pdf"'
        },
    )
