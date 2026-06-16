from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.domain.enums import BodyPart, JobStatus, QAFlag
from app.domain.models import (
    AuditEntry,
    JobRun,
    Result,
    ResultArtifact,
    RoutingRule,
    Series,
    Study,
    UseCase,
)


class TestStudy:
    def test_minimal_construction(self):
        s = Study(study_instance_uid="1.2.3")
        assert s.study_instance_uid == "1.2.3"
        assert s.patient_id is None
        assert s.series == []
        assert isinstance(s.created_at, datetime)
        assert isinstance(s.updated_at, datetime)

    def test_full_construction(self):
        s = Study(
            study_instance_uid="1.2.3.4",
            patient_id="PAT001",
            patient_name="John Doe",
            study_description="BRAIN MRI",
            modality="MR",
            body_part_examined=BodyPart.BRAIN,
            institution_name="Test Hospital",
        )
        assert s.patient_id == "PAT001"
        assert s.body_part_examined == BodyPart.BRAIN
        assert s.modality == "MR"

    def test_created_at_is_timezone_aware(self):
        s = Study(study_instance_uid="1.2.3")
        assert s.created_at.tzinfo is not None
        assert s.created_at.tzinfo == timezone.utc

    def test_series_default_is_empty_list(self):
        s1 = Study(study_instance_uid="1")
        s2 = Study(study_instance_uid="2")
        # Ensure no shared mutable default
        s1.series.append(Series(series_instance_uid="x", study_instance_uid="1"))
        assert len(s2.series) == 0


class TestSeries:
    def test_construction(self):
        s = Series(
            series_instance_uid="1.2.3.1",
            study_instance_uid="1.2.3",
            series_description="T1 MPRAGE",
            modality="MR",
            body_part_examined="BRAIN",
        )
        assert s.series_instance_uid == "1.2.3.1"
        assert s.num_instances == 0
        assert s.dicom_tags == {}

    def test_defaults(self):
        s = Series(series_instance_uid="1", study_instance_uid="2")
        assert s.series_number is None
        assert s.slice_thickness is None
        assert s.pixel_spacing is None


class TestJobRun:
    def test_defaults(self):
        j = JobRun()
        assert j.status == JobStatus.PENDING
        assert j.priority == 0
        assert j.progress == 0.0
        assert j.retry_count == 0
        assert j.error_detail is None
        assert j.started_at is None
        assert j.completed_at is None

    def test_auto_id(self):
        j1 = JobRun()
        j2 = JobRun()
        assert j1.id != j2.id
        uuid.UUID(j1.id)  # validates it's a valid UUID

    def test_timezone_aware(self):
        j = JobRun()
        assert j.created_at.tzinfo == timezone.utc


class TestResult:
    def test_defaults(self):
        r = Result()
        assert r.version == 1
        assert r.is_latest is True
        assert r.qa_flags == []
        assert r.artifacts == []
        assert r.summary == {}

    def test_auto_id(self):
        r = Result()
        uuid.UUID(r.id)

    def test_with_artifacts(self):
        art = ResultArtifact(
            name="seg.nii.gz",
            artifact_type="segmentation_nifti",
            storage_path="1.2.3/brain/seg.nii.gz",
            content_type="application/gzip",
            size_bytes=2048,
        )
        r = Result(artifacts=[art])
        assert len(r.artifacts) == 1
        assert r.artifacts[0].size_bytes == 2048

    def test_with_qa_flags(self):
        r = Result(qa_flags=[QAFlag.MISSING_SEQUENCE, QAFlag.LOW_RESOLUTION])
        assert len(r.qa_flags) == 2
        assert QAFlag.MISSING_SEQUENCE in r.qa_flags


class TestResultArtifact:
    def test_construction(self):
        a = ResultArtifact(
            name="report.json",
            artifact_type="report_json",
            storage_path="path/to/report.json",
        )
        assert a.content_type == "application/octet-stream"
        assert a.size_bytes == 0


class TestUseCase:
    def test_construction(self):
        uc = UseCase(
            name="brain_mri",
            version="1.0.0",
            supported_body_parts=["BRAIN", "HEAD"],
            required_sequences=["T1", "FLAIR"],
            model_type="segresnet",
        )
        assert uc.enabled is True
        assert uc.description == ""
        assert uc.module_path == ""

    def test_disabled(self):
        uc = UseCase(
            name="spine_mri",
            version="0.1.0",
            supported_body_parts=["SPINE"],
            required_sequences=["T2"],
            model_type="segresnet",
            enabled=False,
        )
        assert uc.enabled is False


class TestAuditEntry:
    def test_defaults(self):
        ae = AuditEntry()
        assert ae.actor == "system"
        assert ae.details == {}
        uuid.UUID(ae.id)

    def test_timezone_aware(self):
        ae = AuditEntry()
        assert ae.timestamp.tzinfo == timezone.utc


class TestRoutingRule:
    def test_defaults(self):
        rr = RoutingRule(usecase_name="brain_mri")
        assert rr.modality == "MR"
        assert rr.priority == 0
        assert rr.enabled is True
        assert rr.body_parts == []
        assert rr.study_description_patterns == []
        assert rr.series_description_patterns == []

    def test_full(self):
        rr = RoutingRule(
            usecase_name="brain_mri",
            body_parts=["BRAIN", "HEAD"],
            study_description_patterns=["BRAIN.*MRI"],
            modality="MR",
            priority=10,
        )
        assert rr.priority == 10
        assert len(rr.body_parts) == 2
