from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.enums import BodyPart
from app.domain.models import Series, Study


@pytest.fixture
def deps():
    return {
        "study_repo": AsyncMock(),
        "series_repo": AsyncMock(),
        "audit_repo": AsyncMock(),
        "pacs_client": AsyncMock(),
        "dicomweb_client": MagicMock(),
    }


@pytest.fixture
def study_service(deps):
    from app.application.study_service import StudyService

    return StudyService(**deps)


class TestIngestStudy:
    @pytest.mark.asyncio
    async def test_returns_existing_study(self, study_service, deps):
        existing = Study(study_instance_uid="1.2.3", patient_id="P001")
        deps["study_repo"].get_by_uid.return_value = existing

        result = await study_service.ingest_study("1.2.3")

        assert result == existing
        deps["pacs_client"].get_study.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.application.study_service.DICOMwebClient")
    async def test_ingests_new_study(self, mock_dw_cls, study_service, deps):
        deps["study_repo"].get_by_uid.return_value = None
        deps["pacs_client"].get_study.return_value = {}
        deps["pacs_client"].get_series_list.return_value = []

        # Mock extract_tag_value to return None for all
        mock_dw_cls.extract_tag_value = MagicMock(return_value=None)

        result = await study_service.ingest_study("1.2.3.4.5")

        assert result.study_instance_uid == "1.2.3.4.5"
        deps["study_repo"].save.assert_called()
        deps["audit_repo"].save.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.application.study_service.DICOMwebClient")
    async def test_duplicate_race_condition(self, mock_dw_cls, study_service, deps):
        """If save fails with unique constraint, re-fetch the existing study."""
        deps["study_repo"].get_by_uid.side_effect = [None, Study(study_instance_uid="1.2.3")]
        deps["pacs_client"].get_study.return_value = {}
        mock_dw_cls.extract_tag_value = MagicMock(return_value=None)

        # Simulate unique constraint error on first save
        deps["study_repo"].save.side_effect = Exception("unique constraint violated")

        result = await study_service.ingest_study("1.2.3")

        assert result.study_instance_uid == "1.2.3"


class TestGetStudy:
    @pytest.mark.asyncio
    async def test_get_with_series(self, study_service, deps):
        study = Study(study_instance_uid="1.2.3")
        deps["study_repo"].get_by_uid.return_value = study
        deps["series_repo"].list_by_study.return_value = [
            Series(series_instance_uid="1.2.3.1", study_instance_uid="1.2.3"),
        ]

        result = await study_service.get_study("1.2.3")

        assert result is not None
        assert len(result.series) == 1

    @pytest.mark.asyncio
    async def test_get_not_found(self, study_service, deps):
        deps["study_repo"].get_by_uid.return_value = None
        result = await study_service.get_study("1.2.3")
        assert result is None


class TestListStudies:
    @pytest.mark.asyncio
    async def test_list_with_total(self, study_service, deps):
        deps["study_repo"].list_studies.return_value = [Study(study_instance_uid="1.2.3")]
        deps["study_repo"].count.return_value = 1

        studies, total = await study_service.list_studies(0, 50)

        assert len(studies) == 1
        assert total == 1
