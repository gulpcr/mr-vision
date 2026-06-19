from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from app.application.routing_service import RoutingService
from app.application.usecase_registry import UseCaseRegistry
from app.domain.enums import AuditAction, JobStatus
from app.domain.interfaces import (
    AuditRepository,
    JobRepository,
    SeriesRepository,
    StudyRepository,
)
from app.domain.models import AuditEntry, JobRun, Series, Study
from app.infrastructure.queue.tasks import run_usecase_pipeline

logger = structlog.get_logger(__name__)

_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class JobOrchestrator:
    """Orchestrates job creation and dispatches work to Celery workers."""

    def __init__(
        self,
        study_repo: StudyRepository,
        series_repo: SeriesRepository,
        job_repo: JobRepository,
        audit_repo: AuditRepository,
        routing_service: RoutingService,
        registry: UseCaseRegistry,
    ):
        self._study_repo = study_repo
        self._series_repo = series_repo
        self._job_repo = job_repo
        self._audit_repo = audit_repo
        self._routing_service = routing_service
        self._registry = registry

    async def create_jobs_for_study(
        self,
        study_instance_uid: str,
        usecase_names: list[str] | None = None,
        priority: int = 0,
    ) -> list[JobRun]:
        """Create and dispatch inference jobs for a study."""
        study = await self._study_repo.get_by_uid(study_instance_uid)
        if not study:
            raise ValueError(f"Study {study_instance_uid} not found")

        series = await self._series_repo.list_by_study(study_instance_uid)

        if usecase_names:
            for uc_name in usecase_names:
                if uc_name not in self._registry.usecases:
                    raise ValueError(f"Use case '{uc_name}' is not registered")
            matched_usecases = usecase_names
        else:
            matched_usecases = self._routing_service.route_study(study, series)

        if not matched_usecases:
            logger.warning("no_usecases_matched", study_uid=study_instance_uid)
            return []

        jobs = []
        for uc_name in matched_usecases:
            job = JobRun(
                id=str(uuid.uuid4()),
                study_instance_uid=study_instance_uid,
                usecase_name=uc_name,
                status=JobStatus.PENDING,
                priority=priority,
            )
            await self._job_repo.save(job)

            await self._audit_repo.save(
                AuditEntry(
                    action=AuditAction.JOB_CREATED,
                    entity_type="job",
                    entity_id=job.id,
                    details={
                        "study_uid": study_instance_uid,
                        "usecase": uc_name,
                        "priority": priority,
                    },
                )
            )

            try:
                run_usecase_pipeline.apply_async(
                    args=[job.id, study_instance_uid, uc_name],
                    task_id=job.id,
                    priority=priority,
                )
            except Exception as exc:
                # Broker unavailable or other dispatch failure.
                # Mark the job FAILED now so it never silently sits as PENDING.
                logger.error(
                    "celery_dispatch_failed",
                    job_id=job.id,
                    usecase=uc_name,
                    error=str(exc),
                )
                job.status = JobStatus.FAILED
                job.error_detail = f"Failed to queue task: {exc}"
                job.completed_at = datetime.now(timezone.utc)
                await self._job_repo.update(job)
                raise ValueError(
                    f"Job created but could not be dispatched to the worker queue "
                    f"for use case '{uc_name}'. Is the Celery broker (Redis) reachable? "
                    f"Detail: {exc}"
                ) from exc

            # Prometheus metrics
            try:
                from app.infrastructure.metrics import JOB_CREATED_TOTAL
                JOB_CREATED_TOTAL.labels(usecase=uc_name).inc()
            except Exception:
                pass

            logger.info(
                "job_dispatched",
                job_id=job.id,
                study_uid=study_instance_uid,
                usecase=uc_name,
            )
            jobs.append(job)

        return jobs

    async def cancel_job(self, job_id: str) -> JobRun:
        """Cancel a pending or running job."""
        job = await self._job_repo.get_by_id(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if job.status in _TERMINAL_STATUSES:
            raise ValueError(f"Job {job_id} is already in terminal state: {job.status.value}")

        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(timezone.utc)
        job.status_message = "Cancelled by user"
        await self._job_repo.update(job)

        # Revoke the Celery task. terminate=True kills the worker child if the
        # task is already running; if it is still queued, the task id is added to
        # the workers' revoked set so it is discarded when picked up. The DB is
        # already marked CANCELLED above, so the task's own cancellation checks
        # (and the revoked set) ensure it never produces a result.
        try:
            from app.config import get_settings
            from app.infrastructure.queue.celery_app import celery_app

            signal = get_settings().job_cancel_signal
            celery_app.control.revoke(job_id, terminate=True, signal=signal)
        except Exception as exc:
            logger.warning("celery_revoke_failed", job_id=job_id, error=str(exc))

        await self._audit_repo.save(
            AuditEntry(
                action=AuditAction.JOB_CANCELLED,
                entity_type="job",
                entity_id=job_id,
                details={"study_uid": job.study_instance_uid, "usecase": job.usecase_name},
            )
        )

        logger.info("job_cancelled", job_id=job_id)
        return job

    async def retry_job(self, job_id: str) -> JobRun:
        """Retry a failed or cancelled job."""
        job = await self._job_repo.get_by_id(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            raise ValueError(f"Can only retry FAILED or CANCELLED jobs, got: {job.status.value}")

        job.status = JobStatus.PENDING
        job.progress = 0.0
        job.error_detail = None
        job.started_at = None
        job.completed_at = None
        job.worker_id = None
        job.retry_count += 1
        job.status_message = f"Retry #{job.retry_count}"
        await self._job_repo.update(job)

        run_usecase_pipeline.apply_async(
            args=[job.id, job.study_instance_uid, job.usecase_name],
            task_id=job.id,
            priority=job.priority,
        )

        await self._audit_repo.save(
            AuditEntry(
                action=AuditAction.JOB_RETRIED,
                entity_type="job",
                entity_id=job_id,
                details={
                    "study_uid": job.study_instance_uid,
                    "usecase": job.usecase_name,
                    "retry_count": job.retry_count,
                },
            )
        )

        logger.info("job_retried", job_id=job_id, retry_count=job.retry_count)
        return job

    async def get_job(self, job_id: str) -> JobRun | None:
        return await self._job_repo.get_by_id(job_id)

    async def list_jobs_for_study(self, study_instance_uid: str) -> list[JobRun]:
        return await self._job_repo.list_by_study(study_instance_uid)
