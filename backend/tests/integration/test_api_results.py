from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Result, ResultArtifact


@pytest.fixture
def app_client():
    from app.main import create_app
    from app.interface.api import dependencies

    app = create_app()

    mock_result_service = AsyncMock()
    app.dependency_overrides[dependencies.get_result_service] = lambda: mock_result_service

    mock_registry = MagicMock()
    mock_registry.usecases = {}
    mock_routing = MagicMock()
    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    client = TestClient(app)
    return client, mock_result_service


@pytest.fixture
def sample_result():
    return Result(
        id=str(uuid.uuid4()),
        study_instance_uid="1.2.3.4.5",
        usecase_name="brain_mri",
        job_id=str(uuid.uuid4()),
        summary={"tumor_detected": False},
        measurements={"total_volume": 1200.0},
        model_version="1.0.0",
        model_checksum="abc123",
        artifacts=[
            ResultArtifact(
                name="segmentation.nii.gz",
                artifact_type="segmentation_nifti",
                storage_path="1.2.3/brain_mri/segmentation.nii.gz",
                content_type="application/gzip",
                size_bytes=1024,
            )
        ],
        version=1,
        is_latest=True,
    )


class TestGetResult:
    def test_get_latest(self, app_client, sample_result):
        client, svc = app_client
        svc.get_result.return_value = sample_result

        resp = client.get("/api/results/1.2.3.4.5/brain_mri")
        assert resp.status_code == 200
        data = resp.json()
        assert data["usecase_name"] == "brain_mri"
        assert data["version"] == 1
        assert data["is_latest"] is True
        assert len(data["artifacts"]) == 1

    def test_get_specific_version(self, app_client, sample_result):
        client, svc = app_client
        sample_result.version = 2
        sample_result.is_latest = False
        svc.get_result.return_value = sample_result

        resp = client.get("/api/results/1.2.3.4.5/brain_mri?version=2")
        assert resp.status_code == 200
        assert resp.json()["version"] == 2

    def test_get_not_found(self, app_client):
        client, svc = app_client
        svc.get_result.return_value = None

        resp = client.get("/api/results/1.2.3.4.5/brain_mri")
        assert resp.status_code == 404


class TestListResultVersions:
    def test_list_versions(self, app_client, sample_result):
        client, svc = app_client
        v1 = sample_result
        v2 = Result(
            study_instance_uid="1.2.3.4.5",
            usecase_name="brain_mri",
            job_id=str(uuid.uuid4()),
            model_version="1.1.0",
            model_checksum="def456",
            version=2,
            is_latest=True,
        )
        svc.list_result_versions.return_value = [v2, v1]

        resp = client.get("/api/results/1.2.3.4.5/brain_mri/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2


class TestListStudyResults:
    def test_list_results(self, app_client, sample_result):
        client, svc = app_client
        svc.list_results_for_study.return_value = [sample_result]

        resp = client.get("/api/results/1.2.3.4.5")
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_list_empty(self, app_client):
        client, svc = app_client
        svc.list_results_for_study.return_value = []

        resp = client.get("/api/results/1.2.3.4.5")
        assert resp.status_code == 200
        assert resp.json()["results"] == []
