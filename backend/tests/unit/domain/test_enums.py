from __future__ import annotations

import pytest

from app.domain.enums import AuditAction, BodyPart, JobStatus, Modality, QAFlag


class TestJobStatus:
    def test_values(self):
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.ROUTING.value == "routing"
        assert JobStatus.PREPROCESSING.value == "preprocessing"
        assert JobStatus.INFERRING.value == "inferring"
        assert JobStatus.POSTPROCESSING.value == "postprocessing"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.CANCELLED.value == "cancelled"

    def test_member_count(self):
        assert len(JobStatus) == 8

    def test_is_str_enum(self):
        assert isinstance(JobStatus.PENDING, str)
        assert JobStatus.PENDING == "pending"


class TestQAFlag:
    def test_values(self):
        assert QAFlag.MISSING_SEQUENCE.value == "missing_sequence"
        assert QAFlag.MOTION_ARTIFACT.value == "motion_artifact"
        assert QAFlag.LOW_RESOLUTION.value == "low_resolution"

    def test_member_count(self):
        assert len(QAFlag) == 6


class TestModality:
    def test_values(self):
        assert Modality.MR.value == "MR"
        assert Modality.CT.value == "CT"
        assert Modality.PT.value == "PT"


class TestBodyPart:
    def test_common_parts(self):
        assert BodyPart.BRAIN.value == "BRAIN"
        assert BodyPart.SPINE.value == "SPINE"
        assert BodyPart.KNEE.value == "KNEE"

    def test_member_count(self):
        assert len(BodyPart) == 10

    def test_is_str_enum(self):
        assert isinstance(BodyPart.BRAIN, str)


class TestAuditAction:
    def test_job_lifecycle_actions(self):
        assert AuditAction.JOB_CREATED.value == "job_created"
        assert AuditAction.JOB_STARTED.value == "job_started"
        assert AuditAction.JOB_COMPLETED.value == "job_completed"
        assert AuditAction.JOB_FAILED.value == "job_failed"
        assert AuditAction.JOB_CANCELLED.value == "job_cancelled"
        assert AuditAction.JOB_RETRIED.value == "job_retried"

    def test_study_and_result_actions(self):
        assert AuditAction.STUDY_RECEIVED.value == "study_received"
        assert AuditAction.RESULT_STORED.value == "result_stored"
        assert AuditAction.RESULT_VIEWED.value == "result_viewed"

    def test_config_action(self):
        assert AuditAction.CONFIG_CHANGED.value == "config_changed"

    def test_member_count(self):
        assert len(AuditAction) == 10
