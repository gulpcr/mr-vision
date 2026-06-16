from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.enums import JobStatus
from app.domain.models import JobRun, Series, Study, UseCase

# Mock heavy infrastructure modules before importing JobOrchestrator
_mock_tasks_module = MagicMock()
_mock_tasks_module.run_usecase_pipeline = MagicMock()
sys.modules["app.infrastructure.queue.tasks"] = _mock_tasks_module

from app.application.job_orchestrator import JobOrchestrator  # noqa: E402
# Grab a reference to the actual imported name used by the orchestrator module
import app.application.job_orchestrator as _orch_mod
_run_pipeline = _orch_mod.run_usecase_pipeline


@pytest.fixture(autouse=True)
def _reset_task_mock():
    """Reset the mocked Celery task before each test."""
    _run_pipeline.reset_mock()


@pytest.fixture
def deps():
    """Return mock dependencies for JobOrchestrator."""
    return {
        "study_repo": AsyncMock(),
        "series_repo": AsyncMock(),
        "job_repo": AsyncMock(),
        "audit_repo": AsyncMock(),
        "routing_service": MagicMock(),
        "registry": MagicMock(),
    }


@pytest.fixture
def orchestrator(deps):
    return JobOrchestrator(**deps)


@pytest.fixture
def study():
    return Study(study_instance_uid="1.2.3.4.5")


@pytest.fixture
def series_list():
    return [
        Series(series_instance_uid="1.2.3.4.5.1", study_instance_uid="1.2.3.4.5"),
    ]


class TestCreateJobsForStudy:
    @pytest.mark.asyncio
    async def test_study_not_found_raises(self, orchestrator, deps):
        deps["study_repo"].get_by_uid.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await orchestrator.create_jobs_for_study("1.2.3")

    @pytest.mark.asyncio
    async def test_creates_jobs_for_routed_usecases(self, orchestrator, deps, study, series_list):
        deps["study_repo"].get_by_uid.return_value = study
        deps["series_repo"].list_by_study.return_value = series_list
        deps["routing_service"].route_study.return_value = ["brain_mri"]
        deps["registry"].usecases = {"brain_mri": MagicMock()}

        jobs = await orchestrator.create_jobs_for_study("1.2.3.4.5")

        assert len(jobs) == 1
        assert jobs[0].usecase_name == "brain_mri"
        assert jobs[0].status == JobStatus.PENDING
        deps["job_repo"].save.assert_called_once()
        deps["audit_repo"].save.assert_called_once()
        _run_pipeline.apply_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_usecase_names(self, orchestrator, deps, study, series_list):
        deps["study_repo"].get_by_uid.return_value = study
        deps["series_repo"].list_by_study.return_value = series_list
        deps["registry"].usecases = {"brain_mri": MagicMock()}

        jobs = await orchestrator.create_jobs_for_study(
            "1.2.3.4.5", usecase_names=["brain_mri"]
        )

        assert len(jobs) == 1
        deps["routing_service"].route_study.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_usecase_raises(self, orchestrator, deps, study):
        deps["study_repo"].get_by_uid.return_value = study
        deps["registry"].usecases = {}
        with pytest.raises(ValueError, match="not registered"):
            await orchestrator.create_jobs_for_study(
                "1.2.3.4.5", usecase_names=["nonexistent"]
            )

    @pytest.mark.asyncio
    async def test_no_matched_usecases_returns_empty(self, orchestrator, deps, study, series_list):
        deps["study_repo"].get_by_uid.return_value = study
        deps["series_repo"].list_by_study.return_value = series_list
        deps["routing_service"].route_study.return_value = []

        jobs = await orchestrator.create_jobs_for_study("1.2.3.4.5")

        assert jobs == []
        deps["job_repo"].save.assert_not_called()


class TestCancelJob:
    @pytest.mark.asyncio
    async def test_cancel_not_found(self, orchestrator, deps):
        deps["job_repo"].get_by_id.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await orchestrator.cancel_job("nonexistent")

    @pytest.mark.asyncio
    async def test_cancel_already_terminal(self, orchestrator, deps):
        job = JobRun(status=JobStatus.COMPLETED)
        deps["job_repo"].get_by_id.return_value = job
        with pytest.raises(ValueError, match="terminal state"):
            await orchestrator.cancel_job(job.id)

    @pytest.mark.asyncio
    async def test_cancel_pending_job(self, orchestrator, deps):
        job = JobRun(
            study_instance_uid="1.2.3",
            usecase_name="brain_mri",
            status=JobStatus.PENDING,
        )
        deps["job_repo"].get_by_id.return_value = job

        mock_celery_app = MagicMock()
        with patch.dict(sys.modules, {
            "app.infrastructure.queue.celery_app": MagicMock(celery_app=mock_celery_app),
        }):
            result = await orchestrator.cancel_job(job.id)

        assert result.status == JobStatus.CANCELLED
        assert result.completed_at is not None
        deps["job_repo"].update.assert_called_once()
        deps["audit_repo"].save.assert_called_once()


class TestRetryJob:
    @pytest.mark.asyncio
    async def test_retry_not_found(self, orchestrator, deps):
        deps["job_repo"].get_by_id.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await orchestrator.retry_job("nonexistent")

    @pytest.mark.asyncio
    async def test_retry_non_terminal(self, orchestrator, deps):
        job = JobRun(status=JobStatus.PENDING)
        deps["job_repo"].get_by_id.return_value = job
        with pytest.raises(ValueError, match="FAILED or CANCELLED"):
            await orchestrator.retry_job(job.id)

    @pytest.mark.asyncio
    async def test_retry_failed_job(self, orchestrator, deps):
        job = JobRun(
            study_instance_uid="1.2.3",
            usecase_name="brain_mri",
            status=JobStatus.FAILED,
            error_detail="OOM",
            retry_count=0,
        )
        deps["job_repo"].get_by_id.return_value = job

        result = await orchestrator.retry_job(job.id)

        assert result.status == JobStatus.PENDING
        assert result.retry_count == 1
        assert result.error_detail is None
        assert result.progress == 0.0
        _run_pipeline.apply_async.assert_called_once()
        deps["audit_repo"].save.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_cancelled_job(self, orchestrator, deps):
        job = JobRun(status=JobStatus.CANCELLED, retry_count=2)
        deps["job_repo"].get_by_id.return_value = job

        result = await orchestrator.retry_job(job.id)

        assert result.status == JobStatus.PENDING
        assert result.retry_count == 3


class TestGetAndListJobs:
    @pytest.mark.asyncio
    async def test_get_job(self, orchestrator, deps):
        job = JobRun(id="test-id")
        deps["job_repo"].get_by_id.return_value = job
        result = await orchestrator.get_job("test-id")
        assert result.id == "test-id"

    @pytest.mark.asyncio
    async def test_list_jobs_for_study(self, orchestrator, deps):
        jobs = [JobRun(), JobRun()]
        deps["job_repo"].list_by_study.return_value = jobs
        result = await orchestrator.list_jobs_for_study("1.2.3")
        assert len(result) == 2
