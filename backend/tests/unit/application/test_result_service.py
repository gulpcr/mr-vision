from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.application.result_service import ResultService
from app.domain.models import Result


@pytest.fixture
def result_service():
    repo = AsyncMock()
    store = AsyncMock()
    return ResultService(result_repo=repo, artifact_store=store), repo, store


class TestGetResult:
    @pytest.mark.asyncio
    async def test_get_latest(self, result_service):
        svc, repo, _ = result_service
        expected = Result(study_instance_uid="1.2.3", usecase_name="brain_mri")
        repo.get_by_study_and_usecase.return_value = expected

        result = await svc.get_result("1.2.3", "brain_mri")

        assert result == expected
        repo.get_by_study_and_usecase.assert_called_once_with("1.2.3", "brain_mri")

    @pytest.mark.asyncio
    async def test_get_specific_version(self, result_service):
        svc, repo, _ = result_service
        expected = Result(version=2, is_latest=False)
        repo.get_by_study_usecase_version.return_value = expected

        result = await svc.get_result("1.2.3", "brain_mri", version=2)

        assert result.version == 2
        repo.get_by_study_usecase_version.assert_called_once_with("1.2.3", "brain_mri", 2)
        repo.get_by_study_and_usecase.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_result_not_found(self, result_service):
        svc, repo, _ = result_service
        repo.get_by_study_and_usecase.return_value = None

        result = await svc.get_result("1.2.3", "brain_mri")
        assert result is None


class TestListResults:
    @pytest.mark.asyncio
    async def test_list_for_study(self, result_service):
        svc, repo, _ = result_service
        repo.list_by_study.return_value = [Result(), Result()]

        results = await svc.list_results_for_study("1.2.3")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_versions(self, result_service):
        svc, repo, _ = result_service
        repo.list_versions.return_value = [Result(version=1), Result(version=2)]

        results = await svc.list_result_versions("1.2.3", "brain_mri")
        assert len(results) == 2
        repo.list_versions.assert_called_once_with("1.2.3", "brain_mri")


class TestArtifacts:
    @pytest.mark.asyncio
    async def test_get_artifact_data(self, result_service):
        svc, _, store = result_service
        store.get.return_value = b"binary-data"

        data = await svc.get_artifact_data("1.2.3", "brain_mri", "seg.nii.gz")

        assert data == b"binary-data"
        store.get.assert_called_once_with("1.2.3/brain_mri/seg.nii.gz")

    @pytest.mark.asyncio
    async def test_get_artifact_url(self, result_service):
        svc, _, store = result_service
        store.get_presigned_url.return_value = "https://example.com/presigned"

        url = await svc.get_artifact_url("1.2.3", "brain_mri", "seg.nii.gz")

        assert url == "https://example.com/presigned"

    @pytest.mark.asyncio
    async def test_store_artifact(self, result_service):
        svc, _, store = result_service
        store.put.return_value = "1.2.3/brain_mri/report.json"

        path = await svc.store_artifact(
            "1.2.3", "brain_mri", "report.json", b"data", "application/json"
        )

        assert path == "1.2.3/brain_mri/report.json"
        store.put.assert_called_once_with(
            "1.2.3/brain_mri/report.json", b"data", "application/json"
        )
