from __future__ import annotations

from typing import Annotated

import structlog
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

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{study_uid}/{usecase}/consolidated-report")
async def get_consolidated_report(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    refresh: bool = False,
):
    """Write a grounded FINDINGS/CONCLUSIONS report from a screening result's per-level
    flags, using a SEPARATE text model (does not touch the pipeline). The first call
    generates and CACHES it on the result so repeat opens return the identical report
    (the writer is non-deterministic); pass ?refresh=true to regenerate. Falls back to
    the pipeline's own ai_report / raw flags if the writer model is unavailable.
    """
    import re as _re

    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")

    summary = result.summary or {}
    flagged = summary.get("anomaly_findings") or []
    settings = get_settings()

    # Return the cached report so every open is identical (unless ?refresh=true).
    cached = summary.get("consolidated_report")
    if cached and not refresh and cached.get("findings"):
        return {**cached, "cached": True, "flagged_count": len(flagged)}

    consolidated: dict[str, str] | None = None
    used_model: str | None = None
    if settings.medgemma_enabled:
        from app.application.abdomen_report_service import AbdomenReportService
        from app.application.ct_report_regions import region_meta
        from app.infrastructure.llm.medgemma_client import MedGemmaClient

        _region = region_meta(usecase)
        model = summary.get("medgemma_model") or settings.medgemma_model
        client = MedGemmaClient(
            base_url=settings.ollama_base_url,
            model_name=model,
            timeout_s=settings.medgemma_timeout_s,
            force_json=True,
        )
        _clin = None
        try:
            from app.application.onboarding_service import OnboardingService

            _clin = (await OnboardingService(service._result_repo._session)
                     .get_clinical_for_study(study_uid)) or {}
        except Exception:
            _clin = {}
        _demo = ", ".join(str(v) for v in [(_clin or {}).get("sex"), (_clin or {}).get("age_band")] if v) or None
        consolidated = await AbdomenReportService(client).consolidate(
            flagged=flagged,
            study_description=summary.get("study_description"),
            detail=(summary.get("ai_report") or {}).get("findings"),
            measurement=summary.get("mass_measurement"),
            clinical_history=(_clin or {}).get("clinical_history"),
            demographics=_demo,
            region_label=_region["region_label"],
            markers_enabled=bool(_region["markers_enabled"]),
        )
        if consolidated:
            used_model = model

    if not consolidated:
        ai = summary.get("ai_report") or {}
        raw = "; ".join(
            _re.sub(r"^\s*\[[^\]]*\]\s*", "", str(f.get("finding", ""))).strip()
            for f in flagged if f.get("finding")
        )
        consolidated = {
            "findings": (
                str(ai.get("findings", "")).strip()
                or raw
                or "No focal abnormality was flagged on the reviewed axial levels."
            ),
            "conclusions": (
                str(ai.get("impression", "")).strip()
                or ("See findings above; correlation with clinical information advised."
                    if flagged else "No acute focal abnormality flagged on the reviewed levels.")
            ),
        }

    payload = {
        "findings": consolidated["findings"],
        "conclusions": consolidated["conclusions"],
        "model": used_model,
    }

    # Cache onto the result's summary so subsequent opens are identical. Best-effort —
    # never fail the request if the write doesn't go through.
    try:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from app.infrastructure.database.models import ResultRecord

        session = service._result_repo._session
        rec = (
            await session.execute(select(ResultRecord).where(ResultRecord.id == result.id))
        ).scalar_one_or_none()
        if rec is not None:
            new_summary = dict(rec.summary or {})
            new_summary["consolidated_report"] = payload
            rec.summary = new_summary
            flag_modified(rec, "summary")
            await session.commit()
    except Exception as exc:
        logger.warning("consolidated_report_cache_failed", study_uid=study_uid, error=str(exc))

    return {**payload, "grounded": True, "cached": False, "flagged_count": len(flagged)}


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

    # Fetched early so both the AI-report fallback below and the patient_info merge
    # further down can reuse it without querying twice.
    clinical: dict | None = None
    try:
        from app.application.onboarding_service import OnboardingService

        clinical = await OnboardingService(async_session).get_clinical_for_study(study_uid)
    except Exception:
        pass  # clinical lookup is best-effort — never block report generation

    # On-the-fly AI-report fallback: only reached for pet_ct results that predate this
    # feature or were processed while it was disabled (Result.summary lacks "ai_report",
    # which is normally generated once during the pipeline — see tasks.py — and cached
    # there). Not persisted back to the DB (no partial-update path on ResultRepository,
    # only versioned save()), so unlike the cached path this MAY reword across repeat
    # downloads until the study is reprocessed. Never blocks report generation on failure.
    summary_for_pdf = result.summary or {}
    if (
        usecase == "pet_ct"
        and settings.petct_ai_report_enabled
        and settings.gemini_api_key
        and not (isinstance(summary_for_pdf.get("ai_report"), dict))
    ):
        try:
            from app.application.pet_ct_narrative_service import PetCtNarrativeService
            from app.infrastructure.llm.gemini_client import GeminiClient

            images: dict[str, bytes] = {}
            for name in (
                "mip_axial.png", "mip_coronal.png", "mip_sagittal.png",
                "fused_axial.png", "fused_coronal.png", "fused_sagittal.png",
            ):
                try:
                    images[name] = await service.get_artifact_data(study_uid, usecase, name)
                except Exception:
                    continue  # artifact missing (e.g. no CT was available) — skip it

            narr_client = GeminiClient(api_key=settings.gemini_api_key, model_name=settings.gemini_model)
            ai_report = await PetCtNarrativeService(narr_client).generate(
                summary=summary_for_pdf,
                measurements=result.measurements or {},
                images=images,
                clinical_indication=(clinical or {}).get("indication"),
                clinical_history=(clinical or {}).get("clinical_history"),
            )
            if ai_report:
                summary_for_pdf = {**summary_for_pdf, "ai_report": ai_report}
        except Exception as exc:
            logger.warning("petct_ai_report_ondemand_failed", study_uid=study_uid, error=str(exc))

    # Abdomen CT: write FINDINGS/CONCLUSIONS with the separate report-writer model (same
    # as the /consolidated-report endpoint) so the PDF matches the report view. The
    # pipeline flow is untouched — this only reorganizes the stored per-level flags.
    from app.application.ct_report_regions import is_ct_report_usecase, region_meta
    if is_ct_report_usecase(usecase) and settings.medgemma_enabled:
        try:
            _region = region_meta(usecase)
            # Prefer the cached report (written when the web report was first opened) so the
            # PDF is identical to the on-screen report; only generate if not cached yet.
            _cached = summary_for_pdf.get("consolidated_report") or {}
            _consolidated = None
            if _cached.get("findings"):
                _consolidated = {"findings": _cached["findings"], "conclusions": _cached.get("conclusions", "")}
            else:
                from app.application.abdomen_report_service import AbdomenReportService
                from app.infrastructure.llm.medgemma_client import MedGemmaClient

                _model = summary_for_pdf.get("medgemma_model") or settings.medgemma_model
                _client = MedGemmaClient(
                    base_url=settings.ollama_base_url, model_name=_model,
                    timeout_s=settings.medgemma_timeout_s, force_json=True,
                )
                _demo = ", ".join(str(v) for v in [(clinical or {}).get("sex"), (clinical or {}).get("age_band")] if v) or None
                _consolidated = await AbdomenReportService(_client).consolidate(
                    flagged=summary_for_pdf.get("anomaly_findings") or [],
                    study_description=summary_for_pdf.get("study_description"),
                    detail=(summary_for_pdf.get("ai_report") or {}).get("findings"),
                    measurement=summary_for_pdf.get("mass_measurement"),
                    clinical_history=(clinical or {}).get("clinical_history"),
                    demographics=_demo,
                    region_label=_region["region_label"],
                    markers_enabled=bool(_region["markers_enabled"]),
                )
            if _consolidated:
                summary_for_pdf = {**summary_for_pdf, "ai_report": {
                    "findings": _consolidated["findings"],
                    "impression": _consolidated["conclusions"],
                    "disclaimer": (summary_for_pdf.get("ai_report") or {}).get("disclaimer", ""),
                }}
        except Exception as exc:
            logger.warning("abdomen_report_ondemand_failed", study_uid=study_uid, error=str(exc))

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
    # `clinical` was already fetched above (reused for the AI-report fallback).
    try:
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
            # Fall back to intake demographics when DICOM lacks them.
            if clinical.get("age_band") and not patient_info.get("patient_age"):
                patient_info["patient_age"] = clinical["age_band"]
            if clinical.get("sex") and not patient_info.get("patient_sex"):
                patient_info["patient_sex"] = clinical["sex"]
            for _k in ("priority", "region_profile", "body_part"):
                if clinical.get(_k):
                    patient_info[_k] = clinical[_k]
    except Exception:
        pass  # clinical merge is best-effort — never block report generation

    pdf_bytes = generator.generate(
        study_uid=study_uid,
        usecase_name=usecase,
        result={
            "summary": summary_for_pdf,
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
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Content is regenerated per request from whichever result version is
            # currently latest — a browser-cached response could silently keep
            # showing a prior version's findings (e.g. before ai_report was cached
            # or after a reprocess), so this response must never be cached.
            "Cache-Control": "no-store",
        },
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
