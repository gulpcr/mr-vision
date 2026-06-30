from __future__ import annotations

import enum


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    ROUTING = "routing"
    PREPROCESSING = "preprocessing"
    INFERRING = "inferring"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QAFlag(str, enum.Enum):
    # Metadata-based checks (QAService)
    MISSING_SEQUENCE = "missing_sequence"
    SPACING_INCONSISTENCY = "spacing_inconsistency"
    MOTION_ARTIFACT = "motion_artifact"
    LOW_RESOLUTION = "low_resolution"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    SLICE_GAP = "slice_gap"
    # VLM-detected image quality artifacts (Phase 2)
    LOW_SNR = "low_snr"
    FIELD_INHOMOGENEITY = "field_inhomogeneity"
    ALIASING_ARTIFACT = "aliasing_artifact"
    SUSCEPTIBILITY_ARTIFACT = "susceptibility_artifact"
    TRUNCATION_ARTIFACT = "truncation_artifact"
    CHEMICAL_SHIFT_ARTIFACT = "chemical_shift_artifact"
    PARALLEL_IMAGING_ARTIFACT = "parallel_imaging_artifact"


class Modality(str, enum.Enum):
    MR = "MR"
    CT = "CT"
    PT = "PT"


class BodyPart(str, enum.Enum):
    BRAIN = "BRAIN"
    HEAD = "HEAD"
    SPINE = "SPINE"
    CSPINE = "CSPINE"
    TSPINE = "TSPINE"
    LSPINE = "LSPINE"
    KNEE = "KNEE"
    SHOULDER = "SHOULDER"
    ABDOMEN = "ABDOMEN"
    PELVIS = "PELVIS"
    HEART = "HEART"
    CARDIAC = "CARDIAC"
    CHEST = "CHEST"
    THORAX = "THORAX"
    BREAST = "BREAST"


class AuditAction(str, enum.Enum):
    STUDY_RECEIVED = "study_received"
    JOB_CREATED = "job_created"
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"
    JOB_RETRIED = "job_retried"
    RESULT_STORED = "result_stored"
    RESULT_VIEWED = "result_viewed"
    CONFIG_CHANGED = "config_changed"
    USER_LOGIN = "user_login"
    USER_LOGOUT = "user_logout"
    USER_CREATED = "user_created"
    REPORT_GENERATED = "report_generated"
    BATCH_STARTED = "batch_started"
    BATCH_COMPLETED = "batch_completed"
    REVIEW_SUBMITTED = "review_submitted"
    ALERT_TRIGGERED = "alert_triggered"
    DATA_PURGED = "data_purged"
    PHI_DEIDENTIFIED = "phi_deidentified"


class QASeverity(str, enum.Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    RADIOLOGIST = "radiologist"
    TECHNICIAN = "technician"
    VIEWER = "viewer"


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class BatchStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
