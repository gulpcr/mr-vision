from __future__ import annotations

import os
import tempfile
import traceback
from datetime import datetime, timezone
from typing import Any

import structlog
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded, Terminated
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.domain.enums import AuditAction, JobStatus
from app.domain.models import AuditEntry, Result, ResultArtifact
from app.infrastructure.database.models import (
    AuditLogRecord,
    JobRunRecord,
    ResultRecord,
    StudyRecord,
)
from app.infrastructure.orthanc.client import OrthancPACSClient
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.storage.client import MinIOArtifactStore

logger = structlog.get_logger(__name__)

# Transient exceptions that justify automatic retry
_RETRIABLE_ERRORS = (ConnectionError, IOError, TimeoutError, OSError)


def _get_sync_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine)
    return factory()


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
            record.started_at = datetime.now(timezone.utc)
        if status in (JobStatus.COMPLETED, JobStatus.FAILED):
            record.completed_at = datetime.now(timezone.utc)
        record.updated_at = datetime.now(timezone.utc)
        session.commit()


def _is_job_cancelled(session: Session, job_id: str) -> bool:
    """Check if a job has been cancelled (e.g. by the cancel endpoint)."""
    record = session.query(JobRunRecord).filter(JobRunRecord.id == job_id).first()
    return record is not None and record.status == JobStatus.CANCELLED.value


def _save_result(session: Session, result_data: dict[str, Any]):
    # Mark previous latest as not-latest
    existing = (
        session.query(ResultRecord)
        .filter(
            ResultRecord.study_instance_uid == result_data["study_instance_uid"],
            ResultRecord.usecase_name == result_data["usecase_name"],
            ResultRecord.is_latest == True,
        )
        .first()
    )
    next_version = 1
    if existing:
        next_version = existing.version + 1
        existing.is_latest = False

    record = ResultRecord(
        id=result_data["id"],
        study_instance_uid=result_data["study_instance_uid"],
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


def _write_audit(session: Session, action: str, entity_type: str, entity_id: str, details: dict):
    import uuid

    record = AuditLogRecord(
        id=str(uuid.uuid4()),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor="celery_worker",
        details=details,
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
                        )
                    except Exception as e:
                        logger.warning("review_queue_hook_failed", error=str(e))

                # ── 3. Automated prior comparison ─────────────────────────────
                if study.patient_id:
                    try:
                        from app.infrastructure.database.models import ResultRecord, StudyRecord
                        from sqlalchemy import select as _select
                        # Find prior results for same patient + usecase
                        prior_stmt = (
                            _select(ResultRecord)
                            .join(StudyRecord, ResultRecord.study_instance_uid == StudyRecord.study_instance_uid)
                            .where(
                                StudyRecord.patient_id == study.patient_id,
                                ResultRecord.usecase_name == usecase_name,
                                ResultRecord.is_latest == True,
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
                                details={
                                    "prior_result_id": prior_result.id,
                                    "patient_id": study.patient_id,
                                    "usecase": usecase_name,
                                },
                            )
                            async_session.add(audit)
                    except Exception as e:
                        logger.warning("prior_comparison_hook_failed", error=str(e))

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

    try:
        # Guard: study must still exist (handles post-reset orphaned queue entries)
        study_exists = (
            session.query(StudyRecord)
            .filter(StudyRecord.study_instance_uid == study_instance_uid)
            .first()
        ) is not None
        if not study_exists:
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

                    # Fetch prior results for same patient + usecase via sync session
                    prior_records = (
                        session.query(ResultRecord)
                        .join(
                            _StudyRecord,
                            ResultRecord.study_instance_uid == _StudyRecord.study_instance_uid,
                        )
                        .filter(
                            _StudyRecord.patient_id == study_domain.patient_id,
                            ResultRecord.usecase_name == usecase_name,
                            ResultRecord.is_latest == True,  # noqa: E712
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

            _save_result(session, result_data)

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


@celery_app.task(
    name="app.infrastructure.queue.tasks.run_retention_cleanup",
    max_retries=1,
)
def run_retention_cleanup():
    """Celery Beat task: apply data retention policies (F15)."""
    import asyncio
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

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(
    name="app.infrastructure.queue.tasks.run_critical_alert_escalation",
    max_retries=1,
)
def run_critical_alert_escalation():
    """Celery Beat task: escalate unacknowledged CRITICAL alerts past threshold."""
    import asyncio
    from app.infrastructure.database.session import async_session_factory

    async def _run():
        from app.application.alerting_service import AlertingService
        from app.config import get_settings

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

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


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
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=_STALE_MINUTES)
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

        now = datetime.now(timezone.utc).replace(tzinfo=None)
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
