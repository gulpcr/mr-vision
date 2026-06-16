from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import JobStatus
from app.domain.models import JobRun


@pytest.fixture
def app_client():
    from app.main import create_app
    from app.interface.api import dependencies

    app = create_app()

    mock_orchestrator = AsyncMock()
    mock_job_repo = AsyncMock()

    app.dependency_overrides[dependencies.get_job_orchestrator] = lambda: mock_orchestrator
    app.dependency_overrides[dependencies.get_job_repo] = lambda: mock_job_repo

    # Set up required globals
    mock_registry = MagicMock()
    mock_registry.usecases = {}
    mock_routing = MagicMock()
    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    client = TestClient(app)
    return client, mock_orchestrator, mock_job_repo


@pytest.fixture
def sample_job():
    return JobRun(
        id=str(uuid.uuid4()),
        study_instance_uid="1.2.3.4.5",
        usecase_name="brain_mri",
        status=JobStatus.PENDING,
    )


class TestCreateJobs:
    def test_create_success(self, app_client, sample_job):
        client, orch, _ = app_client
        orch.create_jobs_for_study.return_value = [sample_job]

        resp = client.post(
            "/api/studies/1.2.3.4.5/jobs",
            json={"priority": 0},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["status"] == "pending"

    def test_create_with_usecases(self, app_client, sample_job):
        client, orch, _ = app_client
        orch.create_jobs_for_study.return_value = [sample_job]

        resp = client.post(
            "/api/studies/1.2.3.4.5/jobs",
            json={"usecase_names": ["brain_mri"], "priority": 5},
        )
        assert resp.status_code == 201

    def test_create_study_not_found(self, app_client):
        client, orch, _ = app_client
        orch.create_jobs_for_study.side_effect = ValueError("Study not found")

        resp = client.post(
            "/api/studies/1.2.3/jobs",
            json={"priority": 0},
        )
        assert resp.status_code == 400


class TestGetJob:
    def test_get_existing(self, app_client, sample_job):
        client, _, repo = app_client
        repo.get_by_id.return_value = sample_job

        resp = client.get(f"/api/jobs/{sample_job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sample_job.id
        assert data["retry_count"] == 0

    def test_get_not_found(self, app_client):
        client, _, repo = app_client
        repo.get_by_id.return_value = None

        resp = client.get("/api/jobs/nonexistent-id")
        assert resp.status_code == 404


class TestListStudyJobs:
    def test_list_jobs(self, app_client, sample_job):
        client, orch, _ = app_client
        orch.list_jobs_for_study.return_value = [sample_job]

        resp = client.get("/api/studies/1.2.3.4.5/jobs")
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 1

    def test_list_empty(self, app_client):
        client, orch, _ = app_client
        orch.list_jobs_for_study.return_value = []

        resp = client.get("/api/studies/1.2.3/jobs")
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []


class TestCancelJob:
    def test_cancel_success(self, app_client, sample_job):
        client, orch, _ = app_client
        sample_job.status = JobStatus.CANCELLED
        orch.cancel_job.return_value = sample_job

        resp = client.post(f"/api/jobs/{sample_job.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_already_terminal(self, app_client):
        client, orch, _ = app_client
        orch.cancel_job.side_effect = ValueError("already in terminal state")

        resp = client.post("/api/jobs/some-id/cancel")
        assert resp.status_code == 400


class TestRetryJob:
    def test_retry_success(self, app_client, sample_job):
        client, orch, _ = app_client
        sample_job.status = JobStatus.PENDING
        sample_job.retry_count = 1
        orch.retry_job.return_value = sample_job

        resp = client.post(f"/api/jobs/{sample_job.id}/retry")
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending"
        assert data["retry_count"] == 1

    def test_retry_not_retryable(self, app_client):
        client, orch, _ = app_client
        orch.retry_job.side_effect = ValueError("Can only retry FAILED or CANCELLED")

        resp = client.post("/api/jobs/some-id/retry")
        assert resp.status_code == 400
