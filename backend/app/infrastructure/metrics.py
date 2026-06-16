from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# -- Job lifecycle metrics --
JOB_CREATED_TOTAL = Counter(
    "mri_jobs_created_total",
    "Total inference jobs created",
    ["usecase"],
)

JOB_COMPLETED_TOTAL = Counter(
    "mri_jobs_completed_total",
    "Total inference jobs that reached a terminal state",
    ["usecase", "status"],
)

JOB_DURATION_SECONDS = Histogram(
    "mri_job_duration_seconds",
    "End-to-end job execution time in seconds",
    ["usecase"],
    buckets=[10, 30, 60, 120, 300, 600, 1800, 3600],
)

JOB_QUEUE_DEPTH = Gauge(
    "mri_job_queue_depth",
    "Number of jobs currently in a given status",
    ["status"],
)

# -- Pipeline error metrics --
INFERENCE_ERRORS_TOTAL = Counter(
    "mri_inference_errors_total",
    "Total pipeline errors by use case",
    ["usecase"],
)

# -- Study metrics --
STUDY_INGESTED_TOTAL = Counter(
    "mri_studies_ingested_total",
    "Total studies ingested from PACS",
)
