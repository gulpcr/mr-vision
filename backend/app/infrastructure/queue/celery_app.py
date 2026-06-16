from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "mri_platform",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

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
    task_routes={
        "app.infrastructure.queue.tasks.run_usecase_pipeline": {"queue": "mri_inference"},
        "app.infrastructure.queue.tasks.run_retention_cleanup": {"queue": "celery"},
        "app.infrastructure.queue.tasks.run_critical_alert_escalation": {"queue": "celery"},
        "app.infrastructure.queue.tasks.run_stale_job_cleanup": {"queue": "celery"},
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
    },
)

# Multi-GPU queues (F8)
gpu_queues = [q.strip() for q in settings.gpu_worker_queues.split(",") if q.strip()]
for gpu_queue in gpu_queues:
    celery_app.conf.task_routes[f"app.infrastructure.queue.tasks.run_usecase_pipeline_gpu_{gpu_queue}"] = {
        "queue": gpu_queue
    }

celery_app.autodiscover_tasks(["app.infrastructure.queue"])
