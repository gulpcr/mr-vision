import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.application.job_orchestrator import JobOrchestrator
from app.infrastructure.database.repositories import PgJobRepository
from app.interface.api.dependencies import get_job_orchestrator, get_job_repo
from app.interface.schemas.job import CreateJobRequest, JobListResponse, JobResponse

router = APIRouter(tags=["jobs"])


@router.post("/studies/{study_uid}/jobs", response_model=JobListResponse, status_code=201)
async def create_jobs(
    study_uid: str,
    body: CreateJobRequest,
    orchestrator: Annotated[JobOrchestrator, Depends(get_job_orchestrator)],
):
    try:
        jobs = await orchestrator.create_jobs_for_study(
            study_instance_uid=study_uid,
            usecase_names=body.usecase_names,
            priority=body.priority,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Job creation failed: {e}")

    if not jobs:
        raise HTTPException(
            status_code=400,
            detail=(
                "No matching use cases found for this study. "
                "Check the study's modality, body part, and study description — "
                "they must match the routing rules for at least one enabled pipeline, "
                "or select a specific pipeline manually."
            ),
        )

    return JobListResponse(jobs=[_to_response(j) for j in jobs])


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    job_repo: Annotated[PgJobRepository, Depends(get_job_repo)],
):
    job = await job_repo.get_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _to_response(job)


@router.get("/studies/{study_uid}/jobs", response_model=JobListResponse)
async def list_study_jobs(
    study_uid: str,
    orchestrator: Annotated[JobOrchestrator, Depends(get_job_orchestrator)],
):
    jobs = await orchestrator.list_jobs_for_study(study_uid)
    return JobListResponse(jobs=[_to_response(j) for j in jobs])


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: str,
    orchestrator: Annotated[JobOrchestrator, Depends(get_job_orchestrator)],
):
    """Cancel a pending or running job."""
    try:
        job = await orchestrator.cancel_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(job)


@router.post("/jobs/{job_id}/retry", response_model=JobResponse, status_code=201)
async def retry_job(
    job_id: str,
    orchestrator: Annotated[JobOrchestrator, Depends(get_job_orchestrator)],
):
    """Retry a failed or cancelled job."""
    try:
        job = await orchestrator.retry_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(job)


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: str,
    job_repo: Annotated[PgJobRepository, Depends(get_job_repo)],
):
    """Stream job progress updates via Server-Sent Events (SSE).

    Sends status/progress events every 2 seconds until the job reaches a
    terminal state (completed, failed, cancelled), then closes.
    """
    job = await job_repo.get_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    terminal = {"completed", "failed", "cancelled"}

    async def event_generator():
        last_status = None
        last_progress = None
        keepalive_counter = 0

        for _ in range(900):  # max ~30 min at 2s intervals
            current_job = await job_repo.get_by_id(job_id)
            if not current_job:
                yield _sse_event({"status": "not_found"}, event="error")
                return

            status = current_job.status.value if hasattr(current_job.status, "value") else current_job.status
            progress = current_job.progress

            # Send update if state changed or every 5th iteration as keepalive
            if status != last_status or progress != last_progress or keepalive_counter >= 5:
                payload = {
                    "job_id": current_job.id,
                    "status": status,
                    "progress": progress,
                    "status_message": current_job.status_message,
                    "worker_id": current_job.worker_id,
                }
                yield _sse_event(payload, event="progress")
                last_status = status
                last_progress = progress
                keepalive_counter = 0

                if status in terminal:
                    yield _sse_event({"status": status, "job_id": current_job.id}, event="done")
                    return
            else:
                keepalive_counter += 1

            await asyncio.sleep(2)

        yield _sse_event({"reason": "timeout"}, event="timeout")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_event(data: dict, event: str = "message") -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _to_response(job) -> JobResponse:
    return JobResponse(
        id=job.id,
        study_instance_uid=job.study_instance_uid,
        usecase_name=job.usecase_name,
        status=job.status.value if hasattr(job.status, "value") else job.status,
        priority=job.priority,
        progress=job.progress,
        status_message=job.status_message,
        worker_id=job.worker_id,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_detail=job.error_detail,
        retry_count=job.retry_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
