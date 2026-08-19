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
    NECK = "NECK"
    LOWER_LIMB = "LOWER_LIMB"
    FACE = "FACE"


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


class ObservationStatus(str, enum.Enum):
    """FHIR R4 Observation.status — mandatory (1..1) on every Observation."""

    REGISTERED = "registered"
    PRELIMINARY = "preliminary"
    FINAL = "final"
    AMENDED = "amended"
    CORRECTED = "corrected"
    CANCELLED = "cancelled"
    ENTERED_IN_ERROR = "entered-in-error"
    UNKNOWN = "unknown"


class ObservationCategory(str, enum.Enum):
    """FHIR observation-category codes used by this platform.

    http://terminology.hl7.org/CodeSystem/observation-category — the full value set is
    larger; only the members this platform actually produces are listed.
    """

    VITAL_SIGNS = "vital-signs"
    LABORATORY = "laboratory"
    IMAGING = "imaging"
    SURVEY = "survey"


class ConditionClinicalStatus(str, enum.Enum):
    """FHIR R4 Condition.clinicalStatus (condition-clinical value set)."""

    ACTIVE = "active"
    RECURRENCE = "recurrence"
    RELAPSE = "relapse"
    INACTIVE = "inactive"
    REMISSION = "remission"
    RESOLVED = "resolved"


class ConditionVerificationStatus(str, enum.Enum):
    """FHIR R4 Condition.verificationStatus (condition-ver-status value set).

    A referral indication is asserted by the referrer, not established by this platform,
    so intake-captured conditions default to ``unconfirmed`` rather than ``confirmed``.
    """

    UNCONFIRMED = "unconfirmed"
    PROVISIONAL = "provisional"
    DIFFERENTIAL = "differential"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    ENTERED_IN_ERROR = "entered-in-error"


class ConditionCategory(str, enum.Enum):
    """FHIR condition-category. An imaging referral reason is an encounter diagnosis."""

    PROBLEM_LIST_ITEM = "problem-list-item"
    ENCOUNTER_DIAGNOSIS = "encounter-diagnosis"


class Laterality(str, enum.Enum):
    """Side an Observation applies to. Not a FHIR value set — FHIR expresses this with a
    coded Observation.bodySite — but the platform's own findings are recorded per side
    (mammography), so the side is stored explicitly and mapped on export."""

    LEFT = "left"
    RIGHT = "right"
    BILATERAL = "bilateral"


class AuditActorType(str, enum.Enum):
    """What kind of actor an audit entry attributes an action to.

    Maps to FHIR ``AuditEvent.agent.type``: a person, an algorithm, or the platform
    itself. Previously indistinguishable — one free-text ``actor`` column held all three.
    """

    PRACTITIONER = "practitioner"
    DEVICE = "device"
    SYSTEM = "system"


class AuditAction2(str, enum.Enum):
    """FHIR ``AuditEvent.action`` — a FIXED five-code set, not extensible.

    The platform's own 20-value vocabulary (:class:`AuditAction`) is
    ``AuditEvent.subtype``; this is the coarse verb that makes "every *read* of this
    record" a single indexed query.
    """

    CREATE = "C"
    READ = "R"
    UPDATE = "U"
    DELETE = "D"
    EXECUTE = "E"


# Domain action → AuditEvent.action. An action absent here records NULL rather than being
# forced into a bucket it does not belong in. Kept in sync with migration 032's copy —
# the migration keeps its own frozen copy on purpose, since migrations must not change
# behaviour when this file evolves.
AUDIT_ACTION_TO_CRUDE: dict[str, str] = {
    "study_received": "C",
    "job_created": "C",
    "job_started": "E",
    "job_completed": "E",
    "job_failed": "E",
    "job_cancelled": "U",
    "job_retried": "E",
    "result_stored": "C",
    "result_viewed": "R",
    "report_generated": "R",
    "report_downloaded": "R",
    "result_exported": "R",
    "share_link_redeemed": "R",
    "config_changed": "U",
    "user_login": "E",
    "user_logout": "E",
    "user_created": "C",
    "batch_started": "E",
    "batch_completed": "E",
    "review_submitted": "U",
    "alert_triggered": "C",
    "data_purged": "D",
    "phi_deidentified": "U",
    "patient_created": "C",
    "patient_updated": "U",
    "order_created": "C",
    "order_updated": "U",
    "order_linked_study": "U",
    "study_claimed": "U",
    "study_assigned": "U",
    "study_auto_assigned": "U",
    "study_unclaimed": "U",
    "study_reported": "U",
    "study_signed": "U",
    "study_deleted": "D",
    "orthanc_study_deleted": "D",
    "mammography_report_saved": "U",
    "role_created": "C",
    "role_updated": "U",
    "role_deleted": "D",
}


def audit_action_to_crude(action: str) -> str | None:
    """Coarse FHIR AuditEvent.action for a domain action, or None if unmapped."""
    return AUDIT_ACTION_TO_CRUDE.get(str(action or "").strip().lower())


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
