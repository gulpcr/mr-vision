from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import BodyPart
from app.domain.models import Series, Study


@pytest.fixture
def app_client():
    """Create a FastAPI TestClient with dependency overrides."""
    from app.main import create_app
    from app.interface.api import dependencies

    app = create_app()

    mock_study_service = AsyncMock()
    mock_job_orchestrator = AsyncMock()

    app.dependency_overrides[dependencies.get_study_service] = lambda: mock_study_service
    app.dependency_overrides[dependencies.get_job_orchestrator] = lambda: mock_job_orchestrator

    # Set up registry and routing service to avoid RuntimeError
    mock_registry = MagicMock()
    mock_registry.usecases = {}
    mock_routing = MagicMock()
    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    client = TestClient(app)
    return client, mock_study_service, mock_job_orchestrator


@pytest.fixture
def sample_study():
    return Study(
        study_instance_uid="1.2.3.4.5.6.7.8.9",
        patient_id="PAT001",
        patient_name="Test Patient",
        study_description="BRAIN MRI",
        modality="MR",
        body_part_examined=BodyPart.BRAIN,
    )


class TestListStudies:
    def test_list_empty(self, app_client):
        client, svc, _ = app_client
        svc.list_studies.return_value = ([], 0)

        resp = client.get("/api/studies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["studies"] == []
        assert data["total"] == 0

    def test_list_with_results(self, app_client, sample_study):
        client, svc, _ = app_client
        sample_study.series = []
        svc.list_studies.return_value = ([sample_study], 1)

        resp = client.get("/api/studies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["studies"]) == 1
        assert data["studies"][0]["study_instance_uid"] == "1.2.3.4.5.6.7.8.9"

    def test_list_with_filters(self, app_client):
        client, svc, _ = app_client
        svc.list_studies.return_value = ([], 0)

        resp = client.get("/api/studies?body_part=brain&modality=MR&patient_id=P001")
        assert resp.status_code == 200
        # Verify filters were passed
        call_args = svc.list_studies.call_args
        assert call_args[0][2]["body_part_examined"] == "BRAIN"
        assert call_args[0][2]["modality"] == "MR"


class TestGetStudy:
    def test_get_existing(self, app_client, sample_study):
        client, svc, _ = app_client
        sample_study.series = [
            Series(
                series_instance_uid="1.2.3.4.5.6.7.8.9.1",
                study_instance_uid="1.2.3.4.5.6.7.8.9",
                series_description="T1 MPRAGE",
                modality="MR",
            )
        ]
        svc.get_study.return_value = sample_study

        resp = client.get("/api/studies/1.2.3.4.5.6.7.8.9")
        assert resp.status_code == 200
        data = resp.json()
        assert data["study_instance_uid"] == "1.2.3.4.5.6.7.8.9"
        assert len(data["series"]) == 1

    def test_get_not_found(self, app_client):
        client, svc, _ = app_client
        svc.get_study.return_value = None

        resp = client.get("/api/studies/1.2.3.4.5")
        assert resp.status_code == 404

    def test_invalid_dicom_uid(self, app_client):
        client, _, _ = app_client
        resp = client.get("/api/studies/invalid-uid!")
        assert resp.status_code == 422


class TestIngestStudy:
    def test_ingest_success(self, app_client, sample_study):
        client, svc, _ = app_client
        sample_study.series = []
        svc.ingest_study.return_value = sample_study

        resp = client.post(
            "/api/studies",
            json={"study_instance_uid": "1.2.3.4.5.6.7.8.9"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["study_instance_uid"] == "1.2.3.4.5.6.7.8.9"

    def test_ingest_invalid_uid(self, app_client):
        client, _, _ = app_client
        resp = client.post(
            "/api/studies",
            json={"study_instance_uid": "not-a-uid"},
        )
        assert resp.status_code == 422

    def test_ingest_pacs_error(self, app_client):
        client, svc, _ = app_client
        svc.ingest_study.side_effect = ValueError("Study not found in PACS")

        resp = client.post(
            "/api/studies",
            json={"study_instance_uid": "1.2.3.4.5"},
        )
        assert resp.status_code == 404
