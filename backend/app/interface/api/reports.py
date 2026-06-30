from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cds_service import ClinicalDecisionService
from app.application.llm_report_service import LLMReportService
from app.application.longitudinal_service import LongitudinalAnalysisService
from app.application.result_service import ResultService
from app.config import get_settings
from app.interface.api.dependencies import (
    get_cds_service,
    get_llm_report_service,
    get_longitudinal_service,
    get_result_service,
    get_session,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{study_uid}/{usecase}/clinical-context")
async def get_clinical_context(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    cds_service: Annotated[ClinicalDecisionService, Depends(get_cds_service)],
):
    """Generate (or re-generate) clinical decision support context for an existing result."""
    settings = get_settings()
    if not settings.cds_enabled:
        raise HTTPException(400, "Clinical decision support is not enabled (set CDS_ENABLED=true)")

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    if not cds_service.available:
        raise HTTPException(503, "CDS service unavailable — check GEMINI_API_KEY and google-generativeai install")

    # Return stored context if already present, otherwise generate on the fly
    stored = result.summary.get("clinical_context") if result.summary else None
    if stored and isinstance(stored, dict):
        return {"study_uid": study_uid, "usecase": usecase, "clinical_context": stored, "source": "stored"}

    clinical_context = await cds_service.generate_clinical_context(
        usecase_name=usecase,
        summary=result.summary,
        measurements=result.measurements,
        qa_flags=result.qa_flags,
    )
    return {"study_uid": study_uid, "usecase": usecase, "clinical_context": clinical_context, "source": "generated"}


@router.get("/{study_uid}/{usecase}/longitudinal")
async def get_longitudinal_analysis(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    longitudinal_service: Annotated[LongitudinalAnalysisService, Depends(get_longitudinal_service)],
):
    """Return (or regenerate) the longitudinal trend analysis for a result.

    Returns stored analysis if already present, otherwise queries prior results and
    generates a new analysis on the fly. Requires LONGITUDINAL_ENABLED=true.
    """
    settings = get_settings()
    if not settings.longitudinal_enabled:
        raise HTTPException(400, "Longitudinal analysis is not enabled (set LONGITUDINAL_ENABLED=true)")

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    if not longitudinal_service.available:
        raise HTTPException(503, "Longitudinal service unavailable — check GEMINI_API_KEY")

    # Return stored analysis if already present
    stored = result.summary.get("longitudinal_analysis") if result.summary else None
    if stored and isinstance(stored, dict):
        return {
            "study_uid": study_uid,
            "usecase": usecase,
            "longitudinal_analysis": stored,
            "source": "stored",
        }

    # On-the-fly generation: fetch prior results via the result service
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.interface.api.dependencies import get_session
    from app.infrastructure.database.models import ResultRecord, StudyRecord
    from sqlalchemy import select

    # We need the raw DB session — pull it from the result service's repo
    result_repo = service._result_repo  # PgResultRepository holds the session
    async_session = result_repo._session

    # Fetch the study's patient_id
    study_stmt = select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
    study_res = await async_session.execute(study_stmt)
    study_record = study_res.scalar_one_or_none()
    patient_id = study_record.patient_id if study_record else None

    prior_timepoints: list[dict] = []
    if patient_id:
        prior_stmt = (
            select(ResultRecord)
            .join(StudyRecord, ResultRecord.study_instance_uid == StudyRecord.study_instance_uid)
            .where(
                StudyRecord.patient_id == patient_id,
                ResultRecord.usecase_name == usecase,
                ResultRecord.is_latest == True,  # noqa: E712
                ResultRecord.study_instance_uid != study_uid,
            )
            .order_by(ResultRecord.created_at.asc())
            .limit(settings.longitudinal_max_prior_studies)
        )
        prior_res = await async_session.execute(prior_stmt)
        for r in prior_res.scalars():
            prior_timepoints.append({
                "study_instance_uid": r.study_instance_uid,
                "created_at": r.created_at.isoformat() if r.created_at else "unknown",
                "measurements": r.measurements or {},
                "summary": r.summary or {},
            })

    longitudinal_analysis = await longitudinal_service.analyze(
        usecase_name=usecase,
        current_measurements=result.measurements or {},
        current_summary=result.summary or {},
        prior_timepoints=prior_timepoints,
    )
    return {
        "study_uid": study_uid,
        "usecase": usecase,
        "longitudinal_analysis": longitudinal_analysis,
        "source": "generated",
        "prior_studies_found": len(prior_timepoints),
    }


@router.get("/{study_uid}/{usecase}/narrative")
async def get_narrative_impression(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    llm_service: Annotated[LLMReportService, Depends(get_llm_report_service)],
):
    """Generate and return an AI narrative impression for a result (text only)."""
    settings = get_settings()
    if not settings.llm_enabled:
        raise HTTPException(400, "LLM report generation is not enabled (set LLM_ENABLED=true)")

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    if not llm_service.available:
        raise HTTPException(503, "LLM service unavailable — check GEMINI_API_KEY and google-generativeai install")

    narrative = await llm_service.generate_impression(
        usecase_name=usecase,
        summary=result.summary,
        measurements=result.measurements,
        qa_flags=result.qa_flags,
    )
    return {"study_uid": study_uid, "usecase": usecase, "narrative": narrative}


@router.get("/{study_uid}/{usecase}/pdf")
async def generate_pdf_report(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    llm_service: Annotated[LLMReportService, Depends(get_llm_report_service)],
):
    """Generate and download a PDF report, with AI narrative if LLM is enabled."""
    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    settings = get_settings()
    narrative = ""
    if settings.llm_enabled and llm_service.available:
        narrative = await llm_service.generate_impression(
            usecase_name=usecase,
            summary=result.summary,
            measurements=result.measurements,
            qa_flags=result.qa_flags,
        )

    from app.reports.pdf_generator import PDFReportGenerator, build_petct_patient_info

    # Pull patient/study demographics from the Study record for the report header.
    from sqlalchemy import select

    from app.infrastructure.database.models import StudyRecord

    async_session = service._result_repo._session
    study_rec = (
        await async_session.execute(
            select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
        )
    ).scalar_one_or_none()

    generator = PDFReportGenerator()
    patient_info = build_petct_patient_info(study_rec)
    patient_info["study_uid"] = study_uid

    # Reading-workflow status for the report (unclaimed / reading by / signed off).
    if study_rec is not None:
        patient_info["reading_status"] = getattr(study_rec, "reading_status", "unread") or "unread"
        patient_info["assigned_to_username"] = getattr(study_rec, "assigned_to_username", None)
        _signed = getattr(study_rec, "signed_at", None)
        patient_info["signed_at"] = _signed.strftime("%d/%m/%Y") if _signed else ""

    # Merge clinical intake (patient onboarding) so it appears in the report.
    try:
        from app.application.onboarding_service import OnboardingService

        clinical = await OnboardingService(async_session).get_clinical_for_study(study_uid)
        if clinical:
            if clinical.get("indication"):
                patient_info["indication"] = clinical["indication"]
            if clinical.get("clinical_history"):
                patient_info["clinical_history"] = clinical["clinical_history"]
            if clinical.get("comparative_study"):
                patient_info["comparative_study"] = clinical["comparative_study"]
            if clinical.get("referrer") and not patient_info.get("referring_physician"):
                patient_info["referring_physician"] = clinical["referrer"]
            if clinical.get("fasting_glucose"):
                patient_info["fasting_glucose"] = clinical["fasting_glucose"]
            if clinical.get("injection_site"):
                patient_info["injection_site"] = clinical["injection_site"]
            if clinical.get("creatinine"):
                patient_info["creatinine"] = clinical["creatinine"]
            if clinical.get("bmi"):
                patient_info["bmi"] = f"{clinical['bmi']:g}"
            if clinical.get("height_cm") and not patient_info.get("patient_height"):
                patient_info["patient_height"] = f"{clinical['height_cm']:g}"
            if clinical.get("weight_kg") and not patient_info.get("patient_weight"):
                patient_info["patient_weight"] = f"{clinical['weight_kg']:g}"
            # Fall back to coarse age band when DICOM age is absent.
            if clinical.get("age_band") and not patient_info.get("patient_age"):
                patient_info["patient_age"] = clinical["age_band"]
            for _k in ("priority", "region_profile", "body_part"):
                if clinical.get(_k):
                    patient_info[_k] = clinical[_k]
    except Exception:
        pass  # clinical merge is best-effort — never block report generation

    pdf_bytes = generator.generate(
        study_uid=study_uid,
        usecase_name=usecase,
        result={
            "summary": result.summary,
            "measurements": result.measurements,
            "qa_flags": [f.value if hasattr(f, "value") else f for f in result.qa_flags],
            "qa_details": result.qa_details,
            "model_version": result.model_version,
            "model_checksum": result.model_checksum,
        },
        patient_info=patient_info,
        narrative=narrative,
    )

    filename = f"report_{study_uid[:20]}_{usecase}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{study_uid}/{usecase}/dicom-sr")
async def generate_dicom_sr(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """Generate a DICOM Structured Report."""
    from app.config import get_settings
    settings = get_settings()
    if not settings.dicom_sr_enabled:
        raise HTTPException(400, "DICOM SR generation is not enabled")

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    from app.dicom.sr_generator import SRGenerator

    generator = SRGenerator()
    sr_bytes = generator.generate_sr(
        study_instance_uid=study_uid,
        usecase_name=usecase,
        result={
            "summary": result.summary,
            "measurements": result.measurements,
        },
    )

    filename = f"sr_{study_uid[:20]}_{usecase}.dcm"
    return Response(
        content=sr_bytes,
        media_type="application/dicom",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{study_uid}/{usecase}/fhir")
async def export_fhir_report(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """Export result as FHIR DiagnosticReport."""
    from app.config import get_settings
    settings = get_settings()
    if not settings.fhir_enabled:
        raise HTTPException(400, "FHIR export is not enabled")

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    from app.fhir.fhir_export_service import FHIRExportService

    fhir_service = FHIRExportService()
    report = await fhir_service.export_result(
        study_instance_uid=study_uid,
        usecase_name=usecase,
        result={
            "summary": result.summary,
            "measurements": result.measurements,
            "qa_flags": [f.value if hasattr(f, "value") else f for f in result.qa_flags],
        },
    )
    return report


@router.get("/worklist")
async def query_worklist(
    modality: str = "MR",
    scheduled_date: str | None = None,
):
    """Query scheduled procedures from DICOM Worklist SCP."""
    from app.config import get_settings
    settings = get_settings()
    if not settings.worklist_enabled:
        raise HTTPException(400, "Worklist integration is not enabled")

    from app.dicom.worklist_client import WorklistClient

    client = WorklistClient()
    items = await client.query_worklist(
        scheduled_date=scheduled_date,
        modality=modality,
    )
    return {"items": items}
