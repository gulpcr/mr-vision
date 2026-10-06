from __future__ import annotations

import multiprocessing

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

# The worker pool is --pool=threads (see backend/Dockerfile): every task runs as a
# thread inside ONE OS process, so CUDA gets initialized once and is then live across
# every thread. Any library a task calls that spawns its own multiprocessing.Pool with
# the platform default context — fork() on Linux — forks a process that already holds
# a CUDA context, which reliably crashes/hangs the child and has been observed to take
# the whole Celery MainProcess down with it (TotalSegmentator's internal saving pool via
# a bare `from multiprocessing import Pool`). Forcing "spawn" as the ambient default
# start method here, before any task or CUDA use, makes every such bare Pool()/Process()
# call spawn a clean interpreter instead of forking — safe with an active CUDA context.
# Must run before any CUDA initialization; harmless if already set (e.g. re-import).
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass

settings = get_settings()

celery_app = Celery(
    "mri_platform",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

# TLS to Redis (rediss:// URLs + REDIS_TLS / REDIS_CA_CERT, see infrastructure/tls.py).
from app.infrastructure.tls import celery_ssl_options  # noqa: E402

_redis_ssl = celery_ssl_options(settings)
if _redis_ssl:
    celery_app.conf.broker_use_ssl = _redis_ssl
    celery_app.conf.redis_backend_use_ssl = _redis_ssl

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_default_queue="mri_inference",
    # All maintenance tasks route to "mri_inference" — the only queue any worker
    # in this deployment actually consumes (backend/Dockerfile's worker target
    # runs `celery worker ... -Q mri_inference` with no separate "celery"-queue
    # worker). Routing them to the default "celery" queue silently orphaned them:
    # Beat published run_stale_job_cleanup/run_retention_cleanup/
    # run_critical_alert_escalation on schedule, but nothing ever consumed that
    # queue, so they never ran — e.g. a job stuck since 2026-06-18 was never
    # cleaned up despite the 30-minute staleness check existing and working fine.
    task_routes={
        "app.infrastructure.queue.tasks.run_usecase_pipeline": {"queue": "mri_inference"},
        "app.infrastructure.queue.tasks.run_retention_cleanup": {"queue": "mri_inference"},
        "app.infrastructure.queue.tasks.run_critical_alert_escalation": {"queue": "mri_inference"},
        "app.infrastructure.queue.tasks.run_stale_job_cleanup": {"queue": "mri_inference"},
        "app.infrastructure.queue.tasks.process_batch_item": {"queue": "mri_inference"},
    },
    broker_connection_retry_on_startup=True,
    # Celery Beat schedule (F15 retention, F12 worklist polling)
    beat_schedule={
        "retention-cleanup-daily": {
            "task": "app.infrastructure.queue.tasks.run_retention_cleanup",
            "schedule": 86400.0,  # every 24 hours
        },
        "critical-alert-escalation": {
            "task": "app.infrastructure.queue.tasks.run_critical_alert_escalation",
            "schedule": 300.0,  # every 5 minutes
        },
        "stale-job-cleanup": {
            "task": "app.infrastructure.queue.tasks.run_stale_job_cleanup",
            "schedule": 600.0,  # every 10 minutes
        },
        # HIPAA 164.308(a)(1)(ii)(D): last month's audit-log review, 02:00 UTC on the 1st.
        "audit-review-monthly": {
            "task": "app.infrastructure.queue.tasks.run_audit_review_summary",
            "schedule": (crontab(minute=0, hour=2, day_of_week=1)
                         if settings.audit_review_cadence == "weekly"
                         else crontab(minute=0, hour=2, day_of_month=1)),
        },
        # ADM-04 / PRV-05: security alert rules over the audit log.
        "security-alerts": {
            "task": "app.infrastructure.queue.tasks.run_security_alerts",
            "schedule": 600.0,
        },
        "audit-chain-check": {
            "task": "app.infrastructure.queue.tasks.run_audit_chain_check",
            "schedule": 3600.0,
        },
        # PRV-06: last month's audit log into WORM storage, 03:00 UTC on the 1st.
        "audit-archive-monthly": {
            "task": "app.infrastructure.queue.tasks.run_audit_archive_export",
            "schedule": crontab(minute=0, hour=3, day_of_month=1),
        },
    },
)

# Multi-GPU queues (F8)
gpu_queues = [q.strip() for q in settings.gpu_worker_queues.split(",") if q.strip()]
for gpu_queue in gpu_queues:
    celery_app.conf.task_routes[f"app.infrastructure.queue.tasks.run_usecase_pipeline_gpu_{gpu_queue}"] = {
        "queue": gpu_queue
    }

celery_app.autodiscover_tasks(["app.infrastructure.queue"])
