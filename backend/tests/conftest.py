from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import JobStatus
from app.domain.models import JobRun, Result, ResultArtifact, Series, Study, UseCase


# ---- Reusable fixtures ----


@pytest.fixture
def sample_study() -> Study:
    return Study(
        study_instance_uid="1.2.3.4.5.6.7.8.9",
        patient_id="PAT001",
        patient_name="Test Patient",
        study_description="BRAIN MRI",
        modality="MR",
    )


@pytest.fixture
def sample_series() -> list[Series]:
    return [
        Series(
            series_instance_uid="1.2.3.4.5.6.7.8.9.1",
            study_instance_uid="1.2.3.4.5.6.7.8.9",
            series_description="T1 MPRAGE",
            modality="MR",
            body_part_examined="BRAIN",
        ),
        Series(
            series_instance_uid="1.2.3.4.5.6.7.8.9.2",
            study_instance_uid="1.2.3.4.5.6.7.8.9",
            series_description="FLAIR",
            modality="MR",
            body_part_examined="BRAIN",
        ),
    ]


@pytest.fixture
def sample_job() -> JobRun:
    return JobRun(
        id=str(uuid.uuid4()),
        study_instance_uid="1.2.3.4.5.6.7.8.9",
        usecase_name="brain_mri",
        status=JobStatus.PENDING,
    )


@pytest.fixture
def sample_result() -> Result:
    return Result(
        id=str(uuid.uuid4()),
        study_instance_uid="1.2.3.4.5.6.7.8.9",
        usecase_name="brain_mri",
        job_id=str(uuid.uuid4()),
        summary={"tumor_detected": False},
        measurements={"total_volume": 1200.0},
        qa_flags=[],
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


@pytest.fixture
def sample_usecase() -> UseCase:
    return UseCase(
        name="brain_mri",
        version="1.0.0",
        supported_body_parts=["BRAIN", "HEAD"],
        required_sequences=["T1", "FLAIR"],
        model_type="segresnet",
        enabled=True,
        description="Brain MRI segmentation",
    )


# ---- Mock repositories ----


@pytest.fixture
def mock_study_repo():
    repo = AsyncMock()
    repo.save = AsyncMock()
    repo.get_by_uid = AsyncMock(return_value=None)
    repo.list_studies = AsyncMock(return_value=[])
    repo.count = AsyncMock(return_value=0)
    repo.update = AsyncMock()
    return repo


@pytest.fixture
def mock_series_repo():
    repo = AsyncMock()
    repo.save = AsyncMock()
    repo.list_by_study = AsyncMock(return_value=[])
    repo.save_many = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_job_repo():
    repo = AsyncMock()
    repo.save = AsyncMock()
    repo.get_by_id = AsyncMock(return_value=None)
    repo.list_by_study = AsyncMock(return_value=[])
    repo.update = AsyncMock()
    return repo


@pytest.fixture
def mock_audit_repo():
    repo = AsyncMock()
    repo.save = AsyncMock()
    return repo


@pytest.fixture
def mock_result_repo():
    repo = AsyncMock()
    repo.save = AsyncMock()
    repo.get_by_study_and_usecase = AsyncMock(return_value=None)
    repo.get_by_study_usecase_version = AsyncMock(return_value=None)
    repo.list_versions = AsyncMock(return_value=[])
    repo.list_by_study = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_artifact_store():
    store = AsyncMock()
    store.put = AsyncMock(return_value="path/to/artifact")
    store.get = AsyncMock(return_value=b"data")
    store.get_presigned_url = AsyncMock(return_value="https://example.com/artifact")
    store.exists = AsyncMock(return_value=True)
    return store


# ---- FastAPI test client ----


@pytest.fixture
def test_client(
    sample_study, sample_series, sample_job, sample_result, sample_usecase
):
    """Create a FastAPI TestClient with all dependencies mocked."""
    from app.main import create_app
    from app.interface.api import dependencies
    from app.application.usecase_registry import UseCaseRegistry
    from app.application.routing_service import RoutingService

    app = create_app()

    # Mock registry
    mock_registry = MagicMock(spec=UseCaseRegistry)
    mock_registry.usecases = {"brain_mri": sample_usecase}
    mock_registry.get_routing_rules.return_value = [
        {"body_parts": ["BRAIN"], "modality": "MR", "priority": 10, "enabled": True}
    ]
    mock_registry.get_ui_schema.return_value = {}
    mock_registry.get_output_schema.return_value = {}

    mock_routing = MagicMock(spec=RoutingService)
    mock_routing.route_study.return_value = ["brain_mri"]
    mock_routing.get_all_rules.return_value = {}

    dependencies.set_registry(mock_registry)
    dependencies.set_routing_service(mock_routing)

    return TestClient(app)
