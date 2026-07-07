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
    # Structured per-breast finding slots (validated server-side in the service).
    density_right: str | None = Field(None, description="a-d")
    density_left: str | None = Field(None, description="a-d")
    mass_right: str | None = Field(None, description="none|present")
    mass_left: str | None = Field(None, description="none|present")
    calcification_right: str | None = Field(None, description="none|present")
    calcification_left: str | None = Field(None, description="none|present")
    skin_thickening_right: str | None = Field(None, description="none|present")
    skin_thickening_left: str | None = Field(None, description="none|present")
    nipple_retraction_right: str | None = Field(None, description="none|present")
    nipple_retraction_left: str | None = Field(None, description="none|present")
    architectural_distortion_right: str | None = Field(None, description="none|present")
    architectural_distortion_left: str | None = Field(None, description="none|present")
    axillary_nodes_right: str | None = Field(None, description="normal|abnormal")
    axillary_nodes_left: str | None = Field(None, description="normal|abnormal")
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
    # When AI-authored reports are enabled the mammography report is read-only:
    # the definitive report is written by Gemini from the images + model findings and
    # must not be edited (see MAMMOGRAPHY_AI_REPORT_ENABLED). Reject edits explicitly.
    from app.config import get_settings

    if get_settings().mammography_ai_report_enabled:
        raise HTTPException(
            403, "Mammography report is AI-authored and read-only (MAMMOGRAPHY_AI_REPORT_ENABLED)."
        )
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
    """Render the mammography report to PDF.

    When AI-authored reports are enabled, the report is the read-only AI report from the
    latest pipeline result (Gemini-written, non-diagnostic). Otherwise fall back to the
    saved radiologist report record."""
    from app.config import get_settings
    from app.infrastructure.database.models import ResultRecord, StudyRecord
    from app.reports.pdf_generator import PDFReportGenerator, build_petct_patient_info

    settings = get_settings()
    report: dict | None = None

    if settings.mammography_ai_report_enabled:
        latest = (
            await session.execute(
                select(ResultRecord)
                .where(
                    ResultRecord.study_instance_uid == study_uid,
                    ResultRecord.usecase_name == "mammography",
                    ResultRecord.is_latest == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        summary = (latest.summary if latest else None) or {}
        if summary.get("right_breast_findings") or summary.get("left_breast_findings"):
            lat = (summary.get("laterality") or "bilateral").lower()
            scope = {"right": "of the right breast ", "left": "of the left breast "}.get(lat, "of both breasts ")
            report = {
                "laterality": lat,
                "procedure": f"Digital mammography {scope}performed in routine CC and MLO views.",
                "clinical_features": summary.get("clinical_features"),
                "right_breast_findings": summary.get("right_breast_findings"),
                "left_breast_findings": summary.get("left_breast_findings"),
                "opinion": summary.get("opinion"),
                "birads_right": summary.get("birads_right"),
                "birads_left": summary.get("birads_left"),
                # AI-authored: no human signatory; attribute to the model.
                "reviewing_doctor": "AI-generated (Gemini)",
                "reporting_doctor": summary.get("ai_report_disclaimer")
                or "AI-generated — non-diagnostic, requires radiologist verification",
            }

    if report is None:
        # Fall back to the saved radiologist report record (edit mode / no AI report yet).
        report = await MammographyService(session).get_report(study_uid)
    if report is None:
        raise HTTPException(404, "No mammography report available for this study")

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
