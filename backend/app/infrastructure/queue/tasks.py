from __future__ import annotations

import os
import tempfile
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded, Terminated
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.domain.enums import AuditAction, JobStatus
from app.domain.models import AuditEntry, Result, ResultArtifact, utcnow
from app.infrastructure.database.models import (
    AuditLogRecord,
    JobRunRecord,
    ResultRecord,
    StudyRecord,
)
from app.application.ct_report_regions import CT_REPORT_USECASES as _CT_REPORT_USECASES
from app.infrastructure.orthanc.client import OrthancPACSClient
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.storage.client import MinIOArtifactStore
from app.infrastructure.tenant.context import TenantContext, TenantContextService

logger = structlog.get_logger(__name__)

# Transient exceptions that justify automatic retry
_RETRIABLE_ERRORS = (ConnectionError, IOError, TimeoutError, OSError)


def _get_sync_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine)
    return factory()


def _run_async_beat_task(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async Celery-beat body in a dedicated event loop, then drain the
    shared async engine's connection pool.

    The module-level async engine (session.py) pools connections, and an asyncpg
    connection is bound to the event loop that opened it. Every Celery task runs
    in a fresh loop (asyncio.new_event_loop()), so a pooled connection reused by
    the next task raises "attached to a different loop" / "Event loop is closed".
    Disposing the engine after each task drains the pool so the next invocation
    opens a fresh connection on its own loop.
    """
    import asyncio

    from app.infrastructure.database.session import engine

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        try:
            loop.run_until_complete(engine.dispose())
        except Exception:
            pass
        loop.close()


def _update_job_status(
    session: Session,
    job_id: str,
    status: JobStatus,
    progress: float = 0.0,
    message: str = "",
    error: str | None = None,
    worker_id: str | None = None,
):
    record = session.query(JobRunRecord).filter(JobRunRecord.id == job_id).first()
    if record:
        record.status = status.value
        record.progress = progress
        record.status_message = message
        if error:
            record.error_detail = error
        if worker_id:
            record.worker_id = worker_id
        if status == JobStatus.PREPROCESSING:
            record.started_at = utcnow()
        if status in (JobStatus.COMPLETED, JobStatus.FAILED):
            record.completed_at = utcnow()
        record.updated_at = utcnow()
        session.commit()


def _is_job_cancelled(session: Session, job_id: str) -> bool:
    """Check if a job has been cancelled (e.g. by the cancel endpoint)."""
    record = session.query(JobRunRecord).filter(JobRunRecord.id == job_id).first()
    return record is not None and record.status == JobStatus.CANCELLED.value


def _fmt_dicom_age(raw: str | None) -> str | None:
    """'055Y' → '55 years'; pass through age bands like '40-64'; None → None."""
    if not raw:
        return None
    import re as _re

    m = _re.match(r"^\s*0*(\d+)\s*([YMWD])\s*$", str(raw), _re.IGNORECASE)
    if m:
        unit = {"Y": "years", "M": "months", "W": "weeks", "D": "days"}[m.group(2).upper()]
        return f"{int(m.group(1))} {unit}"
    return str(raw).strip() or None


def _ct_technique_desc(series_desc: str | None, study_desc: str | None) -> str | None:
    """Contrast/technique descriptor from DICOM series/study description, so the report
    writer makes technique-appropriate statements (no 'enhancement' on a non-contrast
    study). Returns e.g. 'contrast-enhanced (venous phase)', 'non-contrast', or None."""
    import re as _re

    text = " ".join(x for x in [series_desc, study_desc] if x).lower()
    if not text.strip():
        return None
    non = bool(_re.search(r"non[- ]?contrast|unenhanced|\bplain\b|without contrast|\bn/?c\b", text))
    con = bool(_re.search(
        r"\bce\b|contrast|\bc\+|post[- ]?contrast|enhanced|venous|arterial|portal|"
        r"nephrographic|delayed|angio", text))
    phase = None
    for p, lab in (("arterial", "arterial phase"), ("portal", "portal-venous phase"),
                   ("venous", "venous phase"), ("nephrographic", "nephrographic phase"),
                   ("delayed", "delayed phase")):
        if p in text:
            phase = lab
            break
    if non:  # 'non-contrast' contains 'contrast'; non-contrast wins over the substring match
        return "non-contrast"
    if con:
        return f"contrast-enhanced ({phase})" if phase else "contrast-enhanced"
    return None


def _report_context_for_study(session: Session, study_uid: str) -> dict[str, Any]:
    """Report context from the study (DICOM) + linked patient intake (sync). Returns
    ``{clinical_history, sex, age, demographics}`` — demographics is a ready 'sex, age'
    string for the model. Sex/age prefer DICOM, fall back to intake."""
    from sqlalchemy import select

    from app.infrastructure.database.models import OrderRecord, PatientRecord, StudyRecord

    ctx: dict[str, Any] = {"clinical_history": None, "sex": None, "age": None, "demographics": None}
    try:
        st = session.execute(
            select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
        ).scalar_one_or_none()
        order = session.execute(
            select(OrderRecord).where(OrderRecord.study_instance_uid == study_uid)
            .order_by(OrderRecord.created_at.desc())
        ).scalars().first()
        patient = None
        if order is None and st and st.patient_id:
            order = session.execute(
                select(OrderRecord).join(PatientRecord, OrderRecord.patient_id == PatientRecord.id)
                .where(PatientRecord.patient_ref == st.patient_id)
                .order_by(OrderRecord.created_at.desc())
            ).scalars().first()
        if order is not None:
            patient = session.execute(
                select(PatientRecord).where(PatientRecord.id == order.patient_id)
            ).scalar_one_or_none()
            ctx["clinical_history"] = (order.clinical_history or order.indication or "").strip() or None

        sex = (getattr(st, "patient_sex", None) or (patient.sex if patient else None) or "").strip() or None
        if sex:
            sex = {"m": "male", "f": "female", "o": "other"}.get(sex.lower(), sex)
        age = _fmt_dicom_age(getattr(st, "patient_age", None)) or (patient.age_band if patient else None)
        ctx["sex"], ctx["age"] = sex, age
        ctx["demographics"] = ", ".join(p for p in [sex, age] if p) or None
    except Exception:
        pass
    return ctx


def _save_result(session: Session, result_data: dict[str, Any], tenant_id: str | None = None):
    # Mark previous latest as not-latest (scoped, so this can never flip another
    # tenant's "latest" result for the same study/usecase pair).
    existing_stmt = session.query(ResultRecord).filter(
        ResultRecord.study_instance_uid == result_data["study_instance_uid"],
        ResultRecord.usecase_name == result_data["usecase_name"],
        ResultRecord.is_latest == True,
    )
    if tenant_id:
        existing_stmt = existing_stmt.filter(ResultRecord.tenant_id == tenant_id)
    existing = existing_stmt.first()
    next_version = 1
    if existing:
        next_version = existing.version + 1
        existing.is_latest = False

    record = ResultRecord(
        id=result_data["id"],
        study_instance_uid=result_data["study_instance_uid"],
        tenant_id=tenant_id or "default",
        usecase_name=result_data["usecase_name"],
        job_id=result_data["job_id"],
        summary=result_data["summary"],
        measurements=result_data["measurements"],
        qa_flags=result_data["qa_flags"],
        qa_details=result_data["qa_details"],
        model_version=result_data["model_version"],
        model_checksum=result_data["model_checksum"],
        artifacts=result_data["artifacts"],
        version=next_version,
        is_latest=True,
    )
    session.add(record)
    session.commit()


def _write_audit(
    session: Session,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict,
    tenant_id: str | None = None,
):
    import uuid

    from app.config import derive_secret
    from app.domain.audit_chain import compute_audit_row_hash

    resolved_tenant_id = tenant_id or "default"
    secret = derive_secret("audit-chain-v1")

    tail = (
        session.query(AuditLogRecord.seq, AuditLogRecord.row_hash)
        .filter(AuditLogRecord.seq.isnot(None))
        .order_by(AuditLogRecord.seq.desc())
        .with_for_update()
        .first()
    )
    next_seq = (tail.seq + 1) if tail else 1
    prev_hash = tail.row_hash if tail else None

    row_hash = compute_audit_row_hash(
        secret=secret,
        seq=next_seq,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor="celery_worker",
        details=details,
        tenant_id=resolved_tenant_id,
        prev_hash=prev_hash,
    )

    record = AuditLogRecord(
        id=str(uuid.uuid4()),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor="celery_worker",
        details=details,
        tenant_id=resolved_tenant_id,
        seq=next_seq,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    session.add(record)
    session.commit()


def _run_post_result_hooks(
    session: Session,
    loop,
    study,
    result_id: str,
    usecase_name: str,
    result_data: dict,
    postprocessed: dict,
) -> None:
    """Fire critical finding alerts, confidence-gated review, auto-comparison."""
    import asyncio as _asyncio
    from app.infrastructure.database.session import async_session_factory

    async def _async_hooks():
        async with async_session_factory() as async_session:
            try:
                # ── 1. Critical finding alert ─────────────────────────────────
                from app.application.alerting_service import AlertingService
                alerting = AlertingService(async_session)
                try:
                    await alerting.evaluate_result_alerts(
                        study_instance_uid=result_data["study_instance_uid"],
                        usecase_name=usecase_name,
                        result_id=result_id,
                        measurements=result_data.get("measurements", {}),
                        summary=result_data.get("summary", {}),
                        qa_flags=result_data.get("qa_flags", []),
                        patient_id=study.patient_id,
                        tenant_id=study.tenant_id,
                    )
                except Exception as e:
                    logger.warning("alert_hook_failed", error=str(e))

                # ── 2. Confidence-gated peer review ───────────────────────────
                confidence = postprocessed.get("confidence_score", 1.0)
                threshold = float(postprocessed.get("review_threshold", 0.75))
                if confidence < threshold:
                    from app.application.active_learning_service import ActiveLearningService
                    al = ActiveLearningService(async_session)
                    try:
                        await al.add_to_review_queue(
                            study_instance_uid=result_data["study_instance_uid"],
                            usecase_name=usecase_name,
                            result_id=result_id,
                            confidence_score=confidence,
                            tenant_id=study.tenant_id,
                        )
                    except Exception as e:
                        logger.warning("review_queue_hook_failed", error=str(e))

                # ── 3. Automated prior comparison ─────────────────────────────
                if study.patient_id:
                    try:
                        from app.infrastructure.database.models import ResultRecord, StudyRecord
                        from sqlalchemy import select as _select
                        # Find prior results for same patient + usecase. patient_id is
                        # only unique within a tenant, so the tenant_id filter here is
                        # load-bearing, not cosmetic.
                        prior_stmt = (
                            _select(ResultRecord)
                            .join(StudyRecord, ResultRecord.study_instance_uid == StudyRecord.study_instance_uid)
                            .where(
                                StudyRecord.patient_id == study.patient_id,
                                StudyRecord.tenant_id == study.tenant_id,
                                ResultRecord.usecase_name == usecase_name,
                                ResultRecord.is_latest == True,
                                ResultRecord.tenant_id == study.tenant_id,
                                ResultRecord.id != result_id,
                            )
                            .order_by(ResultRecord.created_at.desc())
                            .limit(1)
                        )
                        prior_res = await async_session.execute(prior_stmt)
                        prior_result = prior_res.scalar_one_or_none()
                        if prior_result:
                            # Store auto-comparison reference in audit log
                            import uuid as _uuid
                            from app.infrastructure.database.models import AuditLogRecord
                            audit = AuditLogRecord(
                                id=str(_uuid.uuid4()),
                                action="auto_prior_comparison",
                                entity_type="result",
                                entity_id=result_id,
                                actor="celery_worker",
                                tenant_id=study.tenant_id,
                                details={
                                    "prior_result_id": prior_result.id,
                                    "patient_id": study.patient_id,
                                    "usecase": usecase_name,
                                },
                            )
                            async_session.add(audit)
                    except Exception as e:
                        logger.warning("prior_comparison_hook_failed", error=str(e))

                # ── 4. Derived Observations (per-plugin whitelist) ────────────
                # Turns whitelisted result values into row-per-concept Observations so
                # they are searchable and trendable. Plugins with no fhir_map.yaml
                # produce nothing, which is the intended default for the
                # narrative-only families. Never allowed to affect the result.
                try:
                    from app.config import get_settings as _get_settings

                    if _get_settings().observations_enabled and study.patient_id:
                        from app.application.observation_service import ObservationService

                        await ObservationService(async_session).record_result_observations(
                            usecase_name=usecase_name,
                            result=result_data,
                            patient_ref=study.patient_id,
                            study_instance_uid=result_data["study_instance_uid"],
                            result_id=result_id,
                            model_version=result_data.get("model_version"),
                            model_checksum=result_data.get("model_checksum"),
                            effective_dt=getattr(study, "study_date", None),
                            tenant_id=getattr(study, "tenant_id", None) or "default",
                        )
                except Exception as e:
                    logger.warning("result_observations_hook_failed", error=str(e))

                await async_session.commit()
            except Exception as e:
                await async_session.rollback()
                logger.warning("post_result_hooks_failed", error=str(e))

    try:
        # Use asyncio.run() to get a fresh event loop — asyncpg connections
        # are bound to the loop they're created on; reusing the pipeline's
        # loop causes "Future attached to a different loop" on cleanup.
        import asyncio as _asyncio_run
        _asyncio_run.run(_async_hooks())
    except Exception as e:
        logger.warning("post_result_hooks_outer_failed", error=str(e))


@celery_app.task(
    bind=True,
    name="app.infrastructure.queue.tasks.run_usecase_pipeline",
    autoretry_for=_RETRIABLE_ERRORS,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_usecase_pipeline(self: Task, job_id: str, study_instance_uid: str, usecase_name: str):
    """Execute a use-case inference pipeline for a study."""
    import asyncio
    import importlib
    import uuid

    session = _get_sync_session()
    worker_id = self.request.hostname or "unknown"
    tenant_token = None

    try:
        # Guard: study must still exist (handles post-reset orphaned queue entries)
        study_record = (
            session.query(StudyRecord)
            .filter(StudyRecord.study_instance_uid == study_instance_uid)
            .first()
        )
        if study_record is None:
            logger.warning(
                "study_not_found_aborting_job",
                job_id=job_id,
                study_uid=study_instance_uid,
            )
            _update_job_status(
                session, job_id, JobStatus.FAILED, progress=0.0,
                message="Study was deleted after this job was queued.",
                error="StudyNotFound",
            )
            return {"job_id": job_id, "status": "failed", "reason": "study_not_found"}

        # Bind the study's tenant to this worker fiber before preprocessing so the
        # before_flush containment hook (infrastructure/database/session.py) stamps
        # every record this task writes with the correct tenant_id. The plan/features
        # fields are irrelevant here (this is a Celery task, not an HTTP request with a
        # subdomain to resolve) — only tenant_id is read by the flush hook.
        tenant_id = study_record.tenant_id or get_settings().default_tenant_id
        tenant_token = TenantContextService.set_context(
            TenantContext(tenant_id=tenant_id, slug=tenant_id, plan="", features=[])
        )

        # Check if cancelled before starting
        if _is_job_cancelled(session, job_id):
            logger.info("job_already_cancelled", job_id=job_id)
            return {"job_id": job_id, "status": "cancelled"}

        logger.info(
            "pipeline_started",
            job_id=job_id,
            study_uid=study_instance_uid,
            usecase=usecase_name,
            worker=worker_id,
            attempt=self.request.retries + 1,
        )

        _update_job_status(
            session, job_id, JobStatus.PREPROCESSING, progress=0.05,
            message="Loading use case pipeline", worker_id=worker_id,
        )

        module_path = f"app.usecases.{usecase_name}.pipeline"
        pipeline_module = importlib.import_module(module_path)
        pipeline = pipeline_module.Pipeline()

        pacs_client = OrthancPACSClient()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            study_metadata = loop.run_until_complete(pacs_client.get_study(study_instance_uid))
            series_list_raw = loop.run_until_complete(pacs_client.get_series_list(study_instance_uid))
        finally:
            loop.run_until_complete(pacs_client.close())

        from app.infrastructure.dicomweb.client import DICOMwebClient

        series_domain = []
        from app.domain.models import Series, Study

        for s in series_list_raw:
            ext = DICOMwebClient.extract_tag_value
            series_domain.append(
                Series(
                    series_instance_uid=ext(s, "SeriesInstanceUID") or "",
                    study_instance_uid=study_instance_uid,
                    series_number=ext(s, "SeriesNumber"),
                    series_description=ext(s, "SeriesDescription"),
                    modality=ext(s, "Modality"),
                    body_part_examined=ext(s, "BodyPartExamined"),
                    protocol_name=ext(s, "ProtocolName"),
                    slice_thickness=ext(s, "SliceThickness"),
                    # Instance count drives CT-vs-scout selection in pet_ct
                    # (_classify_series); a single-slice topogram must not be
                    # chosen as the fusion CT. QIDO returns it as a string.
                    num_instances=int(
                        float(ext(s, "NumberOfSeriesRelatedInstances") or 0)
                    ),
                    dicom_tags={
                        "RepetitionTime": ext(s, "RepetitionTime"),
                        "EchoTime": ext(s, "EchoTime"),
                        "InversionTime": ext(s, "InversionTime"),
                        "MagneticFieldStrength": ext(s, "MagneticFieldStrength"),
                        "FlipAngle": ext(s, "FlipAngle"),
                        "ScanningSequence": ext(s, "ScanningSequence"),
                        "SequenceVariant": ext(s, "SequenceVariant"),
                        "MRAcquisitionType": ext(s, "MRAcquisitionType"),
                        "SequenceName": ext(s, "SequenceName"),
                        "Rows": ext(s, "Rows"),
                        "Columns": ext(s, "Columns"),
                    },
                )
            )

        ext = DICOMwebClient.extract_tag_value
        study_domain = Study(
            study_instance_uid=study_instance_uid,
            tenant_id=tenant_id,
            patient_id=ext(study_metadata, "PatientID"),
            patient_name=ext(study_metadata, "PatientName"),
            study_description=ext(study_metadata, "StudyDescription"),
            body_part_examined=None,
            modality=ext(study_metadata, "Modality"),
        )

        # Check cancellation before expensive preprocessing
        if _is_job_cancelled(session, job_id):
            logger.info("job_cancelled_before_preprocess", job_id=job_id)
            return {"job_id": job_id, "status": "cancelled"}

        _update_job_status(
            session, job_id, JobStatus.PREPROCESSING, progress=0.15,
            message="Running preprocessing",
        )

        with tempfile.TemporaryDirectory(prefix=f"mri_pipeline_{job_id}_") as working_dir:
            pacs_for_pipeline = OrthancPACSClient()
            try:
                preprocessed = pipeline.preprocess(
                    study=study_domain,
                    series=series_domain,
                    working_dir=working_dir,
                    pacs=pacs_for_pipeline,
                    event_loop=loop,
                )
            finally:
                loop.run_until_complete(pacs_for_pipeline.close())

            # ── VLM Image Quality Assessment (Phase 2) ──────────────────────
            vlm_qa_result: dict = {"flags": [], "details": {}}
            settings = get_settings()
            if settings.vlm_qa_enabled and settings.gemini_api_key:
                try:
                    _update_job_status(
                        session, job_id, JobStatus.PREPROCESSING, progress=0.30,
                        message="Running VLM image quality assessment",
                    )
                    from app.application.vlm_qa_service import VLMQAService
                    from app.infrastructure.llm.gemini_client import GeminiClient

                    vlm_client = GeminiClient(
                        api_key=settings.gemini_api_key,
                        model_name=settings.gemini_model,
                    )
                    vlm_service = VLMQAService(
                        client=vlm_client,
                        max_series=settings.vlm_qa_max_series,
                    )
                    vlm_qa_result = loop.run_until_complete(
                        vlm_service.check_working_dir(working_dir=working_dir)
                    )
                    logger.info(
                        "vlm_qa_completed",
                        job_id=job_id,
                        flags=vlm_qa_result["flags"],
                        series_checked=vlm_qa_result["details"].get("series_checked", 0),
                    )
                except Exception as exc:
                    logger.warning("vlm_qa_failed", job_id=job_id, error=str(exc))

            # Check cancellation before inference
            if _is_job_cancelled(session, job_id):
                logger.info("job_cancelled_before_inference", job_id=job_id)
                return {"job_id": job_id, "status": "cancelled"}

            _update_job_status(
                session, job_id, JobStatus.INFERRING, progress=0.40,
                message="Running model inference",
            )

            inference_output = pipeline.infer(preprocessed, working_dir)

            _update_job_status(
                session, job_id, JobStatus.POSTPROCESSING, progress=0.75,
                message="Running postprocessing",
            )

            postprocessed = pipeline.postprocess(inference_output, working_dir)

            # Merge VLM QA flags into postprocessed result (non-destructive)
            if vlm_qa_result["flags"]:
                existing_flags = postprocessed.get("qa_flags", [])
                existing_flag_vals = {
                    f.value if hasattr(f, "value") else f for f in existing_flags
                }
                for flag in vlm_qa_result["flags"]:
                    if flag not in existing_flag_vals:
                        existing_flags.append(flag)
                        existing_flag_vals.add(flag)
                postprocessed["qa_flags"] = existing_flags
                postprocessed.setdefault("qa_details", {})
                postprocessed["qa_details"]["vlm_qa"] = vlm_qa_result["details"]

            # ── LLM Clinical Decision Support (Phase 3) ──────────────────────
            if settings.cds_enabled and settings.gemini_api_key:
                try:
                    _update_job_status(
                        session, job_id, JobStatus.POSTPROCESSING, progress=0.80,
                        message="Generating clinical decision support",
                    )
                    from app.application.cds_service import ClinicalDecisionService
                    from app.infrastructure.llm.gemini_client import GeminiClient

                    cds_client = GeminiClient(
                        api_key=settings.gemini_api_key,
                        model_name=settings.gemini_model,
                    )
                    cds_svc = ClinicalDecisionService(cds_client)
                    clinical_context = loop.run_until_complete(
                        cds_svc.generate_clinical_context(
                            usecase_name=usecase_name,
                            summary=postprocessed.get("summary", {}),
                            measurements=postprocessed.get("measurements", {}),
                            qa_flags=postprocessed.get("qa_flags", []),
                        )
                    )
                    if clinical_context:
                        postprocessed.setdefault("summary", {})
                        postprocessed["summary"]["clinical_context"] = clinical_context
                        logger.info(
                            "cds_stored",
                            job_id=job_id,
                            risk_level=clinical_context.get("risk_level"),
                            urgency=clinical_context.get("urgency"),
                        )
                except Exception as exc:
                    logger.warning("cds_failed", job_id=job_id, error=str(exc))

            # ── LLM Longitudinal Trend Analysis (Phase 4) ─────────────────────
            if settings.longitudinal_enabled and settings.gemini_api_key and study_domain.patient_id:
                try:
                    _update_job_status(
                        session, job_id, JobStatus.POSTPROCESSING, progress=0.83,
                        message="Running longitudinal trend analysis",
                    )
                    from app.application.longitudinal_service import LongitudinalAnalysisService
                    from app.infrastructure.database.models import StudyRecord as _StudyRecord
                    from app.infrastructure.llm.gemini_client import GeminiClient

                    # Fetch prior results for same patient + usecase via sync session.
                    # patient_id is only unique within a tenant, so the tenant_id filter
                    # here is load-bearing, not cosmetic.
                    prior_records = (
                        session.query(ResultRecord)
                        .join(
                            _StudyRecord,
                            ResultRecord.study_instance_uid == _StudyRecord.study_instance_uid,
                        )
                        .filter(
                            _StudyRecord.patient_id == study_domain.patient_id,
                            _StudyRecord.tenant_id == tenant_id,
                            ResultRecord.usecase_name == usecase_name,
                            ResultRecord.is_latest == True,  # noqa: E712
                            ResultRecord.tenant_id == tenant_id,
                            ResultRecord.study_instance_uid != study_instance_uid,
                        )
                        .order_by(ResultRecord.created_at.asc())
                        .limit(settings.longitudinal_max_prior_studies)
                        .all()
                    )

                    prior_timepoints = [
                        {
                            "study_instance_uid": r.study_instance_uid,
                            "created_at": r.created_at.isoformat() if r.created_at else "unknown",
                            "measurements": r.measurements or {},
                            "summary": r.summary or {},
                        }
                        for r in prior_records
                    ]

                    lng_client = GeminiClient(
                        api_key=settings.gemini_api_key,
                        model_name=settings.gemini_model,
                    )
                    lng_svc = LongitudinalAnalysisService(lng_client)
                    longitudinal_analysis = loop.run_until_complete(
                        lng_svc.analyze(
                            usecase_name=usecase_name,
                            current_measurements=postprocessed.get("measurements", {}),
                            current_summary=postprocessed.get("summary", {}),
                            prior_timepoints=prior_timepoints,
                        )
                    )
                    if longitudinal_analysis:
                        postprocessed.setdefault("summary", {})
                        postprocessed["summary"]["longitudinal_analysis"] = longitudinal_analysis
                        logger.info(
                            "longitudinal_stored",
                            job_id=job_id,
                            trend=longitudinal_analysis.get("trend"),
                            studies_compared=longitudinal_analysis.get("studies_compared"),
                        )
                except Exception as exc:
                    logger.warning("longitudinal_failed", job_id=job_id, error=str(exc))

            # ── Mammography report authoring ──────────────────────────────────
            # Preferred: Gemini reads the rendered views TOGETHER with the fine-tuned
            # model's per-breast finding probabilities + clinical context and authors the
            # per-breast findings / opinion / BI-RADS as a radiologist would (inferring
            # from the images, grounded by the model). Gated by mammography_ai_report_enabled.
            # Falls back to the deterministic MammographyNarrativeService (which only
            # rephrases the structured slots) on disablement, no images, or any failure.
            if usecase_name == "mammography":
                narr_summary = postprocessed.get("summary", {})
                ai_report = None
                if settings.mammography_ai_report_enabled and settings.gemini_api_key:
                    try:
                        _update_job_status(
                            session, job_id, JobStatus.POSTPROCESSING, progress=0.84,
                            message="Generating AI mammography report",
                        )
                        from app.application.mammography_radiologist_service import (
                            MammographyRadiologistService,
                        )
                        from app.infrastructure.llm.gemini_client import GeminiClient
                        from app.infrastructure.database.models import OrderRecord

                        images: list[bytes] = []
                        for artifact in postprocessed.get("artifacts", []):
                            if artifact.get("artifact_type") == "mammo_png":
                                with open(artifact["local_path"], "rb") as f:
                                    images.append(f.read())

                        order = (
                            session.query(OrderRecord)
                            .filter(OrderRecord.study_instance_uid == study_instance_uid)
                            .first()
                        )
                        study_rec_mammo = (
                            session.query(StudyRecord)
                            .filter(StudyRecord.study_instance_uid == study_instance_uid)
                            .first()
                        )
                        rad_client = GeminiClient(
                            api_key=settings.gemini_api_key, model_name=settings.gemini_model
                        )
                        ai_report = loop.run_until_complete(
                            MammographyRadiologistService(rad_client).generate(
                                findings=narr_summary.get("findings", {}) or {},
                                laterality=narr_summary.get("laterality", "bilateral"),
                                images=images,
                                clinical_indication=order.indication if order else None,
                                clinical_history=order.clinical_history if order else None,
                                qa_flags=postprocessed.get("qa_flags", []),
                                patient_age=getattr(study_rec_mammo, "patient_age", None) if study_rec_mammo else None,
                                patient_sex=getattr(study_rec_mammo, "patient_sex", None) if study_rec_mammo else None,
                                birads_right=narr_summary.get("birads_right"),
                                birads_left=narr_summary.get("birads_left"),
                            )
                        )
                        if ai_report:
                            postprocessed.setdefault("summary", {})
                            # BI-RADS is intentionally NOT overridden: the platform's
                            # threshold-derived birads_right/birads_left stay authoritative and
                            # the AI report is prompted to report them as-is. Only the narrative
                            # text is taken from the AI radiologist.
                            for key in (
                                "clinical_features", "right_breast_findings",
                                "left_breast_findings", "opinion",
                            ):
                                if ai_report.get(key) is not None:
                                    postprocessed["summary"][key] = ai_report[key]
                            postprocessed["summary"]["ai_report_disclaimer"] = ai_report.get("disclaimer")
                            logger.info(
                                "mammography_ai_report_stored", job_id=job_id, images_sent=len(images)
                            )
                    except Exception as exc:
                        logger.warning("mammography_ai_report_failed", job_id=job_id, error=str(exc))
                        ai_report = None

                # Deterministic fallback (rephrases the structured slots only).
                if not ai_report:
                    try:
                        from app.application.mammography_narrative_service import (
                            MammographyNarrativeService,
                        )

                        narr_client = None
                        if settings.llm_enabled and settings.gemini_api_key:
                            from app.infrastructure.llm.gemini_client import GeminiClient

                            narr_client = GeminiClient(
                                api_key=settings.gemini_api_key,
                                model_name=settings.gemini_model,
                            )
                        narrative = loop.run_until_complete(
                            MammographyNarrativeService(narr_client).generate(
                                findings=narr_summary.get("findings", {}) or {},
                                laterality=narr_summary.get("laterality", "bilateral"),
                                birads_right=narr_summary.get("birads_right"),
                                birads_left=narr_summary.get("birads_left"),
                            )
                        )
                        postprocessed.setdefault("summary", {})
                        for key, value in narrative.items():
                            if value is not None:
                                postprocessed["summary"][key] = value
                        logger.info("mammography_narrative_stored", job_id=job_id)
                    except Exception as exc:
                        logger.warning("mammography_narrative_failed", job_id=job_id, error=str(exc))

            # ── PET-CT AI-authored report findings ────────────────────────────
            # Writes the structured summary["ai_report"] (scan_findings × 4 regions +
            # conclusions + disclaimer) that the UI (MolecularReport.tsx) and PDF render.
            # Two providers, selected by flag — both parse into the SAME ai_report shape
            # (single validator in pet_ct_narrative_service):
            #   • MedGemma (local Ollama, on-prem, no PHI egress) when medgemma_enabled —
            #     reads the Stage-3 numbered coronal roadmap + regional composite crops and
            #     the data-matrix / Findings-First prompt from usecases/pet_ct/prompts.py.
            #   • Gemini otherwise — reads the 6 MIP/fused views via PetCtNarrativeService.
            # Images are still on local disk here (the artifact-upload loop below has not
            # run yet). Non-blocking: any failure leaves the deterministic summary intact.
            petct_ai_wanted = usecase_name == "pet_ct" and (
                settings.medgemma_enabled
                or (settings.petct_ai_report_enabled and settings.gemini_api_key)
            )
            if petct_ai_wanted:
                try:
                    _update_job_status(
                        session, job_id, JobStatus.POSTPROCESSING, progress=0.84,
                        message="Generating AI radiology findings",
                    )
                    from app.application.pet_ct_narrative_service import (
                        _parse_json_response,
                        _validate_and_normalise,
                    )
                    from app.infrastructure.database.models import OrderRecord

                    order = (
                        session.query(OrderRecord)
                        .filter(OrderRecord.study_instance_uid == study_instance_uid)
                        .first()
                    )
                    summ = postprocessed.get("summary", {}) or {}
                    arts = postprocessed.get("artifacts", []) or []
                    ai_report = None
                    ai_provider = None
                    n_images = 0

                    if settings.medgemma_enabled:
                        # Local MedGemma over the Stage-3 roadmap + composite crops, using
                        # the pet_ct prompts.py data-matrix / Findings-First prompt.
                        from app.infrastructure.llm.medgemma_client import MedGemmaClient
                        from app.usecases.pet_ct import prompts

                        road = [a for a in arts if a.get("artifact_type") == "lesion_roadmap_png"]
                        comps = sorted(
                            (a for a in arts if a.get("artifact_type") == "composite_crop_png"),
                            key=lambda a: a.get("name", ""),
                        )
                        imgs: list[tuple[str, bytes]] = []
                        for a in [*road, *comps]:
                            lp = a.get("local_path")
                            if lp and os.path.exists(lp):
                                with open(lp, "rb") as f:
                                    imgs.append((a.get("name", ""), f.read()))
                        payload = prompts.build_payload(
                            summary=summ,
                            measurements=postprocessed.get("measurements", {}),
                            images=imgs,
                            radiopharmaceutical=summ.get("radiopharmaceutical", "FDG"),
                            quantitative=bool(summ.get("quantitative", True)),
                            max_images=settings.medgemma_max_images,
                        )
                        n_images = len(payload["images"])
                        client = MedGemmaClient(
                            base_url=settings.ollama_base_url,
                            model_name=settings.medgemma_model,
                            timeout_s=settings.medgemma_timeout_s,
                            force_json=True,
                        )
                        if client.ready:
                            raw = loop.run_until_complete(
                                client.generate_from_images(payload["prompt"], payload["images"])
                            )
                            ai_report = _validate_and_normalise(_parse_json_response(raw)) if raw else None
                        ai_provider = f"medgemma:{settings.medgemma_model}"
                    else:
                        # Gemini over the 6 MIP/fused views (referring context if recorded).
                        from app.application.pet_ct_narrative_service import PetCtNarrativeService
                        from app.infrastructure.llm.gemini_client import GeminiClient

                        _NAMES = {
                            "mip_axial.png", "mip_coronal.png", "mip_sagittal.png",
                            "fused_axial.png", "fused_coronal.png", "fused_sagittal.png",
                        }
                        images: dict[str, bytes] = {}
                        for a in arts:
                            lp = a.get("local_path")
                            if a.get("name") in _NAMES and lp and os.path.exists(lp):
                                with open(lp, "rb") as f:
                                    images[a["name"]] = f.read()
                        n_images = len(images)
                        narr_client = GeminiClient(
                            api_key=settings.gemini_api_key, model_name=settings.gemini_model,
                        )
                        ai_report = loop.run_until_complete(
                            PetCtNarrativeService(narr_client).generate(
                                summary=summ,
                                measurements=postprocessed.get("measurements", {}),
                                images=images,
                                clinical_indication=order.indication if order else None,
                                clinical_history=order.clinical_history if order else None,
                            )
                        )
                        ai_provider = f"gemini:{settings.gemini_model}"

                    if ai_report:
                        postprocessed.setdefault("summary", {})
                        postprocessed["summary"]["ai_report"] = ai_report
                        postprocessed["summary"]["ai_report_provider"] = ai_provider
                        logger.info(
                            "petct_ai_report_stored", job_id=job_id,
                            provider=ai_provider, images_sent=n_images,
                        )
                except Exception as exc:
                    logger.warning("petct_ai_report_failed", job_id=job_id, error=str(exc))

            # ── Coronary CTA AI-authored report narrative ─────────────────────
            # Gemini reads the calcium-overlay PNGs (still on local disk — the artifact
            # upload loop below hasn't run yet) together with the deterministic
            # Agatston/stenosis findings and writes a synthesized narrative report.
            # Falls back silently to the deterministic diagnosis/processing_notes already
            # set by postprocess() on any failure, disablement, or a malformed/ungrounded
            # response — see coronary_cta_narrative_service.py.
            if (
                usecase_name == "coronary_cta"
                and settings.coronary_cta_ai_report_enabled
                and settings.gemini_api_key
            ):
                try:
                    _update_job_status(
                        session, job_id, JobStatus.POSTPROCESSING, progress=0.84,
                        message="Generating AI radiology findings",
                    )
                    from app.application.coronary_cta_narrative_service import (
                        CoronaryCtaNarrativeService,
                    )
                    from app.infrastructure.llm.gemini_client import GeminiClient

                    images: dict[str, bytes] = {}
                    for artifact in postprocessed.get("artifacts", []):
                        if artifact.get("artifact_type") in ("overlay_png", "stenosis_crop_png"):
                            with open(artifact["local_path"], "rb") as f:
                                images[artifact["name"]] = f.read()

                    from app.infrastructure.database.models import OrderRecord

                    order = (
                        session.query(OrderRecord)
                        .filter(OrderRecord.study_instance_uid == study_instance_uid)
                        .first()
                    )

                    narr_client = GeminiClient(
                        api_key=settings.gemini_api_key,
                        model_name=settings.gemini_model,
                    )
                    ai_report = loop.run_until_complete(
                        CoronaryCtaNarrativeService(narr_client).generate(
                            summary=postprocessed.get("summary", {}),
                            measurements=postprocessed.get("measurements", {}),
                            images=images,
                            qa_flags=postprocessed.get("qa_flags", []),
                            clinical_indication=order.indication if order else None,
                            clinical_history=order.clinical_history if order else None,
                        )
                    )
                    if ai_report:
                        postprocessed.setdefault("summary", {})
                        postprocessed["summary"]["ai_report"] = ai_report
                        logger.info(
                            "coronary_cta_ai_report_stored",
                            job_id=job_id,
                            images_sent=len(images),
                        )
                except Exception as exc:
                    logger.warning("coronary_cta_ai_report_failed", job_id=job_id, error=str(exc))

            # ── Abdomen CT (v2): MedGemma two-pass full-volume scan → report ──
            # The abdomen_ct pipeline renders EVERY foreground axial slice into the
            # working dir and returns a `scan_slices` manifest (+ `scan_config`) as
            # extra keys (not persisted). Pass 1: MedGemma scans the whole volume in
            # batches to flag potentially-abnormal levels. Pass 2: it reports on the
            # flagged levels only. The flagged slices are then appended as artifacts so
            # exactly what MedGemma reported on is uploaded/shown. Gated by
            # medgemma_enabled; non-blocking — any failure leaves the deterministic
            # summary and preview artifacts intact.
            if usecase_name in _CT_REPORT_USECASES and settings.medgemma_enabled:
                try:
                    _update_job_status(
                        session, job_id, JobStatus.POSTPROCESSING, progress=0.84,
                        message="Scanning volume for abnormalities (MedGemma)",
                    )
                    import importlib
                    from app.infrastructure.llm.medgemma_client import MedGemmaClient
                    from app.application.ct_report_regions import region_meta

                    _region = region_meta(usecase_name)
                    # Each CT-report plugin owns its own region-specific report/prompts
                    # module (self-contained); the shared hook drives whichever is running.
                    abdomen_ct_report = importlib.import_module(
                        f"app.usecases.{usecase_name}.report"
                    )

                    manifest = postprocessed.get("scan_slices") or []
                    scan_cfg = postprocessed.get("scan_config") or {}
                    summ = postprocessed.setdefault("summary", {})
                    ct2_model = scan_cfg.get("model") or settings.medgemma_model

                    client = MedGemmaClient(
                        base_url=settings.ollama_base_url,
                        model_name=ct2_model,
                        timeout_s=settings.medgemma_timeout_s,
                        force_json=True,
                    )
                    if manifest and client.ready:
                        scan_windows = scan_cfg.get("scan_windows") or (
                            [scan_cfg.get("scan_window")] if scan_cfg.get("scan_window") else []
                        )
                        report_windows = scan_cfg.get("report_windows") or list(scan_windows)

                        # Read each rendered PNG once; group scan images per scan window
                        # (one per level, in manifest/superior→inferior order) and report
                        # images (all report windows per level, window tagged).
                        _bytes_cache: dict[str, bytes] = {}

                        def _read_png(path: str) -> bytes:
                            if path not in _bytes_cache:
                                with open(path, "rb") as fh:
                                    _bytes_cache[path] = fh.read()
                            return _bytes_cache[path]

                        scan_images_by_window: dict[str, list[dict[str, Any]]] = {
                            w: [] for w in scan_windows
                        }
                        seen_z: dict[str, set[int]] = {w: set() for w in scan_windows}
                        report_images_by_z: dict[int, list[dict[str, Any]]] = {}
                        for e in manifest:
                            lp = e.get("local_path")
                            if not (lp and os.path.exists(lp)):
                                continue
                            z, win, name = int(e["z"]), e.get("window"), e.get("name", "")
                            if win in scan_images_by_window and z not in seen_z[win]:
                                scan_images_by_window[win].append(
                                    {"z": z, "name": name, "bytes": _read_png(lp)}
                                )
                                seen_z[win].add(z)
                            if win in report_windows:
                                report_images_by_z.setdefault(z, []).append(
                                    {"name": name, "window": win, "bytes": _read_png(lp)}
                                )

                        # MRI plugins report the exact sequences present in each montage so
                        # the prompts can forbid the model inventing signal on absent
                        # sequences. CT summaries carry no `sequences_used`, so this stays
                        # empty and the CT report modules (no `sequences` param) are unaffected.
                        _seqs = summ.get("sequences_used") or None
                        _seq_kw = {"sequences": _seqs} if _seqs else {}

                        # Report context (demographics + clinical history), computed BEFORE the
                        # scan so an age-sensitive MRI region (brain_mri) can weight patient age
                        # when flagging. CT summaries have no `sequences_used`, so `_scan_kw` stays
                        # empty and CT's scan_and_report (no sequences/demographics params) is
                        # unaffected; non-age-sensitive MRI regions receive it but ignore it.
                        _ctx = _report_context_for_study(session, study_instance_uid)
                        _demo = _ctx.get("demographics")
                        _clin_hist = _ctx.get("clinical_history")
                        if _clin_hist:
                            summ["clinical_history"] = _clin_hist
                        _scan_kw = {**_seq_kw, "demographics": _demo} if _seqs else {}
                        # EXPERIMENT: age-aware Stage-1 scan for abdomen_ct only (siblings'
                        # scan_and_report has no `demographics` param). Gated so it reverts
                        # to the age-blind prompt with one flag flip.
                        if (
                            not _seqs
                            and usecase_name == "abdomen_ct"
                            and settings.abdomen_scan_age_context
                            and _demo
                        ):
                            _scan_kw = {"demographics": _demo}

                        result = loop.run_until_complete(
                            abdomen_ct_report.scan_and_report(
                                client=client,
                                scan_images_by_window=scan_images_by_window,
                                report_images_by_z=report_images_by_z,
                                study_description=summ.get("study_description"),
                                windows=report_windows,
                                batch_size=int(scan_cfg.get("batch_size", 2)),
                                max_report_levels=int(scan_cfg.get("max_report_levels", 6)),
                                **_scan_kw,
                            )
                        )

                        anomaly_z = result.get("anomaly_z") or []
                        summ["anomaly_slices"] = anomaly_z
                        summ["anomaly_findings"] = result.get("flagged") or []
                        summ["scan_batches"] = result.get("batches")
                        if result.get("ai_report"):
                            summ["ai_report"] = result["ai_report"]
                            summ["ai_report_provider"] = f"medgemma:{ct2_model}"
                            summ["medgemma_model"] = ct2_model

                        # Detailed read: characterization (adjacent-structure relationships)
                        # + systematic organ review over the flagged levels PLUS an evenly-
                        # spread coverage set, so the report reads like a full radiologist
                        # report. Additive — does not affect scanning/flagging. Overwrites
                        # ai_report with the richer version when it succeeds.
                        try:
                            _stw = scan_windows[0] if scan_windows else None
                            _by_z: dict[int, str] = {}
                            for e in manifest:
                                lp = e.get("local_path")
                                if e.get("window") == _stw and lp and os.path.exists(lp):
                                    _by_z.setdefault(int(e["z"]), lp)
                            _all = sorted(_by_z)
                            _cov_n = 10
                            if len(_all) > _cov_n:
                                _st = (len(_all) - 1) / (_cov_n - 1)
                                _cov = {_all[round(i * _st)] for i in range(_cov_n)}
                            else:
                                _cov = set(_all)
                            _pick = sorted(set(anomaly_z) | _cov, reverse=True)[:16]
                            _rr = [{"z": z, "bytes": _read_png(_by_z[z])} for z in _pick if z in _by_z]
                            rich = loop.run_until_complete(
                                abdomen_ct_report.rich_read(
                                    client=client, images=_rr,
                                    flagged=result.get("flagged") or [],
                                    study_description=summ.get("study_description"),
                                    demographics=_demo,
                                    **_seq_kw,
                                )
                            )
                            if rich:
                                summ["ai_report"] = rich
                                summ["ai_report_provider"] = f"medgemma:{ct2_model}"
                                summ["medgemma_model"] = ct2_model
                        except Exception as exc:
                            logger.warning("abdomen_ct_rich_read_failed", job_id=job_id, error=str(exc))

                        # Option A — adversarial VLM verification (DISABLED). On ABIDA/SANIA
                        # it rejected BOTH (0 yes / 3 no each): the "disprove it" framing just
                        # flips the VLM to under-call — it killed the real ovarian mass too. The
                        # VLM cannot discriminate real vs phantom (see report.verify_mass_vlm).
                        # Kept for reference; gated off so it doesn't burn 3 VLM calls/mass.
                        if False and usecase_name == "abdomen_ct":
                            try:
                                import re as _re_mv
                                _MASS_MV = _re_mv.compile(
                                    r"mass|tumou?r|neoplas|adnexal|lesion|carcinoma", _re_mv.IGNORECASE
                                )
                                _mass_flags = [
                                    f for f in (result.get("flagged") or [])
                                    if f.get("finding") and _MASS_MV.search(str(f["finding"]))
                                ]
                                if _mass_flags:
                                    _stw2 = scan_windows[0] if scan_windows else None
                                    _zpng: dict[int, str] = {}
                                    for e in manifest:
                                        lp = e.get("local_path")
                                        if e.get("window") == _stw2 and lp and os.path.exists(lp):
                                            _zpng.setdefault(int(e["z"]), lp)
                                    _mzs = sorted({int(f["z"]) for f in _mass_flags}, reverse=True)
                                    _mimgs = [
                                        {"z": z, "bytes": _read_png(_zpng[z])}
                                        for z in _mzs if z in _zpng
                                    ][:8]
                                    if _mimgs:
                                        _vv = loop.run_until_complete(
                                            abdomen_ct_report.verify_mass_vlm(
                                                client=client, images=_mimgs, flagged=_mass_flags,
                                                study_description=summ.get("study_description"),
                                                demographics=_demo, votes=3,
                                            )
                                        )
                                        if _vv:
                                            summ["mass_verification_vlm"] = _vv
                            except Exception as exc:
                                logger.warning("abdomen_ct_verify_vlm_failed", job_id=job_id, error=str(exc))

                        _organ = None

                        # Tumour measurement (SAM-Med3D): localize the mass, segment it,
                        # and store TS×AP×CC mm in summary["mass_measurement"] for the
                        # report writer to weave in. Opt-in via measure.enabled; fully
                        # non-blocking (needs SAM-Med3D weights + torchio in the worker).
                        measure_cfg = scan_cfg.get("measure") or {}
                        if measure_cfg.get("enabled") and measure_cfg.get("kind") == "spine_geometry":
                            # Level-identification pass (spine): TotalSegmentator-MR labels the
                            # lumbar vertebrae so the report carries a segmentation-verified level
                            # enumeration (objective; the VLM keeps signal/severity description).
                            # Non-blocking — any failure leaves the narrative untouched.
                            try:
                                _spine_measure = importlib.import_module(
                                    f"app.usecases.{usecase_name}.measurement"
                                )
                                _mvol = scan_cfg.get("measure_volume")
                                if _mvol and os.path.exists(_mvol):
                                    _lm = _spine_measure.measure_levels(_mvol, working_dir, measure_cfg)
                                    if _lm:
                                        summ["level_measurements"] = _lm
                                        _mf = _lm.get("measured_findings")
                                        if _mf and summ.get("ai_report"):
                                            _imp = summ["ai_report"].get("impression") or ""
                                            summ["ai_report"]["impression"] = f"{_mf} {_imp}".strip()
                            except Exception as exc:
                                logger.warning("lumbar_spine_measure_failed", job_id=job_id, error=str(exc))
                        elif measure_cfg.get("enabled"):
                            try:
                                _abd_measure = importlib.import_module(
                                    f"app.usecases.{usecase_name}.measurement"
                                )
                                _abd_sam = importlib.import_module(
                                    f"app.usecases.{usecase_name}.sammed3d"
                                )

                                _vol = os.path.join(working_dir, "nifti", "volume.nii.gz")
                                _loc = _abd_measure.mass_localization(
                                    _vol, result.get("flagged") or [], working_dir, measure_cfg
                                )
                                if _loc:
                                    _meas = _abd_sam.segment_and_measure(
                                        _vol, _loc, working_dir, measure_cfg
                                    )
                                    if _meas:
                                        summ["mass_measurement"] = _meas
                            except Exception as exc:
                                logger.warning("abdomen_ct_measure_failed", job_id=job_id, error=str(exc))

                        # Tier-1 organ grounding (abdomen_ct): deterministic organ sizes →
                        # hepatomegaly/splenomegaly/AAA as FACTS, fed to the report-writer as
                        # authoritative ground truth (reports organomegaly the VLM can't
                        # perceive AND anchors it against confabulating a mass over normal-
                        # measured organs). Runs AFTER measurement so it REUSES the same
                        # TotalSeg mask (working_dir/abdomen_measure_seg/organs.nii.gz) —
                        # cache hit = pure-CPU arithmetic, no extra GPU allocation (a second
                        # GPU TotalSeg run here previously exhausted VRAM). Non-blocking.
                        if usecase_name == "abdomen_ct" and settings.organ_grounding_enabled:
                            try:
                                import re as _re_age
                                from app.usecases.abdomen_ct import organ_grounding as _og

                                _age_yr = None
                                _m_age = _re_age.search(r"\d+", str(_ctx.get("age") or ""))
                                if _m_age:
                                    _age_yr = int(_m_age.group())
                                _vol_g = os.path.join(working_dir, "nifti", "volume.nii.gz")
                                _organ = _og.analyze_organs(_vol_g, working_dir, age_years=_age_yr)
                                if _organ:
                                    summ["organ_measurements"] = _organ["measurements"]
                                    if _organ.get("findings"):
                                        summ["organ_findings"] = _organ["findings"]
                            except Exception as exc:
                                logger.warning("abdomen_ct_organ_grounding_failed", job_id=job_id, error=str(exc))

                        # Grounded mass verification (option C): does a coherent unlabeled
                        # soft-tissue mass actually exist at the flagged location, or is the
                        # region normal/organ tissue (a likely over-call)? Deterministic —
                        # reuses the TotalSeg cache, no extra GPU. Verdict drives the writer.
                        _verify = None
                        if usecase_name == "abdomen_ct" and settings.organ_grounding_enabled:
                            try:
                                _vmod = importlib.import_module(
                                    f"app.usecases.{usecase_name}.measurement"
                                )
                                _vol_v = os.path.join(working_dir, "nifti", "volume.nii.gz")
                                _verify = _vmod.verify_mass(
                                    _vol_v, result.get("flagged") or [], working_dir, measure_cfg
                                )
                                if _verify:
                                    summ["mass_verification"] = _verify
                            except Exception as exc:
                                logger.warning("abdomen_ct_verify_failed", job_id=job_id, error=str(exc))

                        # Pre-generate the consolidated Findings/Conclusions report NOW so
                        # opening the report is instant (the /consolidated-report endpoint
                        # returns this cached value instead of writing on first open).
                        try:
                            from app.application.abdomen_report_service import AbdomenReportService

                            _cons = loop.run_until_complete(
                                AbdomenReportService(client).consolidate(
                                    flagged=result.get("flagged") or [],
                                    study_description=summ.get("study_description"),
                                    detail=(summ.get("ai_report") or {}).get("findings"),
                                    measurement=summ.get("mass_measurement"),
                                    clinical_history=_clin_hist,
                                    demographics=_demo,
                                    region_label=_region["region_label"],
                                    markers_enabled=bool(_region["markers_enabled"]),
                                    grounded_facts=(_organ or {}).get("facts_text"),
                                    # Protocol/contrast phase from DICOM → technique-aware
                                    # statements. Clinical history (already passed) is now
                                    # framed by the writer as a present/absent question with
                                    # an open incidental sweep (interpretation layer; the scan
                                    # pass stays context-blind to avoid anchoring).
                                    technique=_ct_technique_desc(
                                        summ.get("series_description"), summ.get("study_description")
                                    ),
                                    # NOTE: the residual-exclusion verifier (verify_mass) is
                                    # NOT used to drive the report — on the ABIDA/SANIA test it
                                    # INVERTED both (rejected a real cystic mass, confirmed a
                                    # phantom over unlabelled bowel/mesentery). The verdict is
                                    # still computed + stored (summary.mass_verification) for
                                    # analysis, but must not influence the narrative until a
                                    # method that separates the cases exists.
                                    verification=None,
                                    skeptic=False,
                                )
                            )
                            if _cons:
                                summ["consolidated_report"] = {
                                    "findings": _cons["findings"],
                                    "conclusions": _cons["conclusions"],
                                    "model": ct2_model,
                                }
                        except Exception as exc:
                            logger.warning("abdomen_ct_consolidate_failed", job_id=job_id, error=str(exc))

                        # Surface the reported-on slices as artifacts (append + dedup by
                        # name) so the UI shows exactly what MedGemma reported on.
                        arts = postprocessed.setdefault("artifacts", [])
                        existing_names = {a.get("name") for a in arts}
                        report_z = set(result.get("report_z") or [])
                        for e in manifest:
                            if int(e["z"]) not in report_z or e.get("window") not in report_windows:
                                continue
                            name = e.get("name", "")
                            if name in existing_names:
                                continue
                            arts.append({
                                "name": name,
                                "artifact_type": f"{usecase_name}_slice_png",
                                "local_path": e.get("local_path"),
                                "content_type": "image/png",
                            })
                            existing_names.add(name)

                        logger.info(
                            "abdomen_ct_ai_report_stored",
                            job_id=job_id, scan_windows=scan_windows,
                            scanned=sum(len(v) for v in scan_images_by_window.values()),
                            flagged=len(anomaly_z), batches=result.get("batches"),
                            has_report=bool(result.get("ai_report")),
                        )
                except Exception as exc:
                    logger.warning("abdomen_ct_ai_report_failed", job_id=job_id, error=str(exc))

            _update_job_status(
                session, job_id, JobStatus.POSTPROCESSING, progress=0.85,
                message="Storing artifacts",
            )

            store = MinIOArtifactStore()
            artifact_records = []
            for artifact in postprocessed.get("artifacts", []):
                artifact_local_path = artifact["local_path"]
                storage_key = f"{study_instance_uid}/{usecase_name}/{artifact['name']}"
                with open(artifact_local_path, "rb") as f:
                    artifact_data = f.read()
                loop.run_until_complete(
                    store.put(storage_key, artifact_data, artifact.get("content_type", "application/octet-stream"))
                )
                artifact_records.append({
                    "name": artifact["name"],
                    "artifact_type": artifact["artifact_type"],
                    "storage_path": storage_key,
                    "content_type": artifact.get("content_type", "application/octet-stream"),
                    "size_bytes": len(artifact_data),
                })

            result_id = str(uuid.uuid4())
            result_data = {
                "id": result_id,
                "study_instance_uid": study_instance_uid,
                "usecase_name": usecase_name,
                "job_id": job_id,
                "summary": postprocessed.get("summary", {}),
                "measurements": postprocessed.get("measurements", {}),
                "qa_flags": postprocessed.get("qa_flags", []),
                "qa_details": postprocessed.get("qa_details", {}),
                "model_version": postprocessed.get("model_version", "unknown"),
                "model_checksum": postprocessed.get("model_checksum", ""),
                "artifacts": artifact_records,
            }

            _save_result(session, result_data, tenant_id=tenant_id)

            # ── DICOM SR/Seg Export (Phase 7) ─────────────────────────────────
            if settings.dicom_sr_enabled or settings.dicom_seg_enabled:
                try:
                    _update_job_status(
                        session, job_id, JobStatus.POSTPROCESSING, progress=0.90,
                        message="Exporting DICOM SR/Seg to PACS",
                    )
                    from app.services.dicom_export_service import DICOMExportService

                    async def _dicom_export():
                        svc = DICOMExportService()
                        return await svc.export_result(
                            study_instance_uid=study_instance_uid,
                            usecase_name=usecase_name,
                            result_data=result_data,
                            export_sr=settings.dicom_sr_enabled,
                            export_seg=settings.dicom_seg_enabled,
                        )

                    exported_ids = loop.run_until_complete(_dicom_export())
                    logger.info(
                        "dicom_export_completed",
                        job_id=job_id,
                        study_uid=study_instance_uid,
                        exported=exported_ids,
                    )
                except Exception as exc:
                    logger.warning("dicom_export_failed", job_id=job_id, error=str(exc))

            # ── Post-result hooks ─────────────────────────────────────────────
            _run_post_result_hooks(
                session=session,
                loop=loop,
                study=study_domain,
                result_id=result_id,
                usecase_name=usecase_name,
                result_data=result_data,
                postprocessed=postprocessed,
            )

        _update_job_status(
            session, job_id, JobStatus.COMPLETED, progress=1.0,
            message="Pipeline completed successfully",
        )

        _write_audit(
            session, AuditAction.JOB_COMPLETED.value, "job", job_id,
            {"study_uid": study_instance_uid, "usecase": usecase_name, "result_id": result_id},
            tenant_id=tenant_id,
        )

        # Prometheus metrics
        try:
            from app.infrastructure.metrics import JOB_COMPLETED_TOTAL, JOB_DURATION_SECONDS

            JOB_COMPLETED_TOTAL.labels(usecase=usecase_name, status="completed").inc()
            record = session.query(JobRunRecord).filter(JobRunRecord.id == job_id).first()
            if record and record.started_at and record.completed_at:
                duration = (record.completed_at - record.started_at).total_seconds()
                JOB_DURATION_SECONDS.labels(usecase=usecase_name).observe(duration)
        except Exception:
            pass

        logger.info(
            "pipeline_completed",
            job_id=job_id,
            study_uid=study_instance_uid,
            usecase=usecase_name,
        )

        return {"job_id": job_id, "status": "completed", "result_id": result_id}

    except _RETRIABLE_ERRORS as exc:
        # Let Celery's autoretry handle these — update status to show retry
        error_detail = traceback.format_exc()
        retries_left = self.max_retries - self.request.retries
        logger.warning(
            "pipeline_transient_error",
            job_id=job_id,
            error=str(exc),
            retries_left=retries_left,
        )
        _update_job_status(
            session, job_id, JobStatus.PENDING, progress=0.0,
            message=f"Retrying ({self.request.retries + 1}/{self.max_retries}) — {str(exc)[:200]}",
        )
        raise  # Celery autoretry will catch and schedule

    except (Terminated, SoftTimeLimitExceeded) as exc:
        # The task was revoked (Stop button) or hit its time limit. This is a
        # cancellation, not a failure — record it as CANCELLED and return so the
        # generic handler below never marks the job FAILED.
        logger.info("pipeline_terminated", job_id=job_id, reason=type(exc).__name__)
        try:
            session.rollback()
            _update_job_status(
                session, job_id, JobStatus.CANCELLED, progress=0.0,
                message="Cancelled by user",
            )
        except Exception:
            logger.warning("failed_to_mark_cancelled_on_terminate", job_id=job_id)
        return {"job_id": job_id, "status": "cancelled"}

    except Exception as exc:
        error_detail = traceback.format_exc()

        # If the job was cancelled while running, the kill can surface as a
        # secondary error (closed PACS/DB connection mid-op). Do NOT overwrite
        # the CANCELLED status with FAILED — honour the user's Stop request.
        try:
            session.rollback()
            if _is_job_cancelled(session, job_id):
                logger.info("pipeline_aborted_after_cancel", job_id=job_id, error=str(exc))
                return {"job_id": job_id, "status": "cancelled"}
        except Exception:
            logger.warning("cancel_recheck_failed", job_id=job_id)

        logger.error(
            "pipeline_failed",
            job_id=job_id,
            study_uid=study_instance_uid,
            usecase=usecase_name,
            error=str(exc),
            traceback=error_detail,
        )

        try:
            _update_job_status(
                session, job_id, JobStatus.FAILED, progress=0.0,
                message=f"Pipeline failed: {str(exc)[:500]}",
                error=error_detail,
            )

            _write_audit(
                session, AuditAction.JOB_FAILED.value, "job", job_id,
                {"study_uid": study_instance_uid, "usecase": usecase_name, "error": str(exc)[:1000]},
                tenant_id=locals().get("tenant_id"),
            )

            # Prometheus metrics
            try:
                from app.infrastructure.metrics import JOB_COMPLETED_TOTAL, INFERENCE_ERRORS_TOTAL

                JOB_COMPLETED_TOTAL.labels(usecase=usecase_name, status="failed").inc()
                INFERENCE_ERRORS_TOTAL.labels(usecase=usecase_name).inc()
            except Exception:
                pass
        except Exception:
            logger.error("failed_to_update_job_status_on_error", job_id=job_id)

        raise

    finally:
        try:
            loop.close()
        except Exception:
            pass
        session.close()
        if tenant_token is not None:
            TenantContextService.reset_context(tenant_token)


@celery_app.task(
    name="app.infrastructure.queue.tasks.run_retention_cleanup",
    max_retries=1,
)
def run_retention_cleanup():
    """Celery Beat task: apply data retention policies (F15)."""
    from app.infrastructure.database.session import async_session_factory

    async def _run():
        from app.application.retention_service import RetentionService

        async with async_session_factory() as session:
            try:
                service = RetentionService(session)
                totals = await service.apply_policies()
                await session.commit()
                logger.info("retention_cleanup_completed", totals=totals)
                return totals
            except Exception as e:
                await session.rollback()
                logger.error("retention_cleanup_failed", error=str(e))
                raise

    return _run_async_beat_task(_run)


@celery_app.task(
    name="app.infrastructure.queue.tasks.run_critical_alert_escalation",
    max_retries=1,
)
def run_critical_alert_escalation():
    """Celery Beat task: escalate unacknowledged CRITICAL alerts past threshold."""
    from app.infrastructure.database.session import async_session_factory

    async def _run():
        from app.application.alerting_service import AlertingService

        threshold_minutes = 30
        async with async_session_factory() as session:
            try:
                svc = AlertingService(session)
                count = await svc.escalate_overdue_alerts(threshold_minutes)
                await session.commit()
                if count:
                    logger.info("critical_alerts_escalated", count=count)
                return count
            except Exception as e:
                await session.rollback()
                logger.error("critical_alert_escalation_failed", error=str(e))
                raise

    return _run_async_beat_task(_run)


@celery_app.task(
    bind=True,
    name="app.infrastructure.queue.tasks.process_batch_item",
    max_retries=2,
    retry_backoff=True,
)
def process_batch_item(
    self: Task,
    batch_id: str,
    study_instance_uid: str,
    usecase_names: list[str] | None = None,
):
    """Process a single item from a batch upload (F19)."""
    import asyncio
    from app.infrastructure.database.session import async_session_factory

    async def _run():
        from app.application.batch_service import BatchUploadService
        from app.application.study_service import StudyService
        from app.infrastructure.database.repositories import (
            PgAuditRepository,
            PgPendingStudyTenantRepository,
            PgSeriesRepository,
            PgStudyRepository,
        )
        from app.infrastructure.dicomweb.client import DICOMwebClient
        from app.infrastructure.orthanc.client import OrthancPACSClient

        async with async_session_factory() as session:
            try:
                batch_service = BatchUploadService(session)
                study_service = StudyService(
                    study_repo=PgStudyRepository(session),
                    series_repo=PgSeriesRepository(session),
                    audit_repo=PgAuditRepository(session),
                    pacs_client=OrthancPACSClient(),
                    dicomweb_client=DICOMwebClient(),
                    pending_tenant_repo=PgPendingStudyTenantRepository(session),
                    unscoped_study_repo=PgStudyRepository(session, tenant_id=None),
                )

                await study_service.ingest_study(study_instance_uid)
                await batch_service.update_item_status(
                    batch_id, study_instance_uid, "completed"
                )
                await session.commit()

                # Notify via WebSocket
                try:
                    from app.interface.api.ws import notify_batch_progress
                    batch = await batch_service.get_batch(batch_id)
                    if batch:
                        await notify_batch_progress(
                            batch_id,
                            batch["completed_items"],
                            batch["total_items"],
                            batch["status"],
                        )
                except Exception:
                    pass

            except Exception as e:
                await session.rollback()
                async with async_session_factory() as err_session:
                    err_service = BatchUploadService(err_session)
                    await err_service.update_item_status(
                        batch_id, study_instance_uid, "failed", str(e)[:500]
                    )
                    await err_session.commit()
                logger.error(
                    "batch_item_failed",
                    batch_id=batch_id,
                    study_uid=study_instance_uid,
                    error=str(e),
                )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(
    name="app.infrastructure.queue.tasks.run_stale_job_cleanup",
    max_retries=1,
)
def run_stale_job_cleanup():
    """Celery Beat task: expire jobs stuck in active states for > 30 minutes.

    Jobs can get stuck in pending/preprocessing/inferring/postprocessing when a
    Celery worker is killed mid-task or when the broker loses the task message.
    This task detects them by checking `updated_at` and marks them FAILED so
    the worklist does not show phantom 'in-progress' indicators forever.
    """
    from datetime import datetime, timedelta, timezone

    _ACTIVE = ("pending", "routing", "preprocessing", "inferring", "postprocessing")
    _STALE_MINUTES = 30

    session = _get_sync_session()
    try:
        cutoff = utcnow() - timedelta(minutes=_STALE_MINUTES)
        stale_records = (
            session.query(JobRunRecord)
            .filter(
                JobRunRecord.status.in_(_ACTIVE),
                JobRunRecord.updated_at < cutoff,
            )
            .all()
        )

        if not stale_records:
            return 0

        now = utcnow()
        for record in stale_records:
            record.status = JobStatus.FAILED.value
            record.error_detail = (
                f"Task expired: no heartbeat for >{_STALE_MINUTES} min. "
                "Worker likely restarted. Re-run the job to retry."
            )
            record.completed_at = now
            record.updated_at = now

        session.commit()
        logger.info("stale_jobs_expired", count=len(stale_records))
        return len(stale_records)

    except Exception as exc:
        session.rollback()
        logger.error("stale_job_cleanup_failed", error=str(exc))
        raise
    finally:
        session.close()
