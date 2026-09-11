from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


from app.domain.enums import (
    AuditAction,
    BodyPart,
    JobStatus,
    Laterality,
    ObservationCategory,
    ObservationStatus,
    QAFlag,
)


def utcnow() -> datetime:
    """The platform's single clock for persisted timestamps: timezone-aware UTC.

    Every DateTime column is ``TIMESTAMP WITH TIME ZONE`` (migration 027), so naive
    datetimes must never be written or compared against one — asyncpg rejects the
    mismatch and any Python-side comparison raises TypeError. Use this everywhere a
    stored timestamp is produced, rather than ``datetime.utcnow()`` (naive, and
    deprecated in 3.12+) or ``datetime.now()`` (naive *local* time).

    DICOM content timestamps are the one deliberate exception: the standard expects
    site-local time in ContentDate/ContentTime, so the SR/Seg generators keep their
    own local clock.
    """
    return datetime.now(timezone.utc)


# Retained so the ~25 `field(default_factory=_utcnow)` declarations below keep reading
# the way they always have; `utcnow` is the name to import from other modules.
_utcnow = utcnow


@dataclass
class Study:
    study_instance_uid: str
    tenant_id: str = "default"
    # patient_id is the raw DICOM PatientID (an identifier in the hospital's namespace);
    # patient_record_id is the resolved reference to the local patients row. Read-only
    # here — the link is owned by OnboardingService, not by study ingest.
    patient_id: str | None = None
    patient_record_id: str | None = None
    patient_name: str | None = None
    patient_sex: str | None = None
    patient_age: str | None = None
    patient_weight_kg: float | None = None
    patient_height_cm: float | None = None
    study_date: datetime | None = None
    # "second" when DICOM StudyTime (0008,0030) was present and combined into
    # study_date; "date" when only StudyDate was available, so the time-of-day is
    # unknown rather than midnight. Consumers must not render or compare the time
    # component when this is "date" (FHIR dateTime permits a bare YYYY-MM-DD).
    study_date_precision: str = "date"
    study_description: str | None = None
    accession_number: str | None = None
    referring_physician: str | None = None
    body_part_examined: BodyPart | None = None
    modality: str | None = None
    institution_name: str | None = None
    orthanc_id: str | None = None
    reading_status: str = "unread"
    assigned_to: str | None = None
    assigned_to_username: str | None = None
    assigned_at: datetime | None = None
    reported_at: datetime | None = None
    signed_at: datetime | None = None
    series: list[Series] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class Series:
    series_instance_uid: str
    study_instance_uid: str
    tenant_id: str = "default"
    series_number: int | None = None
    series_description: str | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    protocol_name: str | None = None
    num_instances: int = 0
    slice_thickness: float | None = None
    pixel_spacing: tuple[float, float] | None = None
    image_orientation: str | None = None
    orthanc_id: str | None = None
    dicom_tags: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class UseCase:
    name: str
    version: str
    supported_body_parts: list[str]
    required_sequences: list[str]
    model_type: str
    enabled: bool = True
    module_path: str = ""
    description: str = ""
    registered_at: datetime = field(default_factory=_utcnow)


@dataclass
class JobRun:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "default"
    study_instance_uid: str = ""
    usecase_name: str = ""
    status: JobStatus = JobStatus.PENDING
    priority: int = 0
    progress: float = 0.0
    status_message: str = ""
    worker_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_detail: str | None = None
    retry_count: int = 0
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class Result:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "default"
    study_instance_uid: str = ""
    usecase_name: str = ""
    job_id: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    measurements: dict[str, Any] = field(default_factory=dict)
    qa_flags: list[QAFlag] = field(default_factory=list)
    qa_details: dict[str, Any] = field(default_factory=dict)
    model_version: str = ""
    model_checksum: str = ""
    artifacts: list[ResultArtifact] = field(default_factory=list)
    version: int = 1
    is_latest: bool = True
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class Observation:
    """One measured or asserted clinical concept — the row-per-value shape FHIR
    ``Observation`` requires.

    Exists because clinical values previously had no addressable home: labs were
    free-text strings on ``orders``, AI measurements were buried in an opaque
    ``results_index.measurements`` JSON blob, and mammography findings were encoded in
    column *names* (``mass_right``). None of those can answer
    ``Observation?patient=X&code=Y&date=ge…``, which is the whole point of the resource.

    Exactly one of ``value_quantity`` / ``value_string`` / ``value_codeable_code`` is
    normally set. A quantity without a UCUM ``value_unit`` is not a measurement, so the
    writer refuses that combination rather than emitting ``unit: "unknown"``.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "default"
    patient_ref: str = ""
    patient_id: str | None = None
    study_instance_uid: str | None = None
    result_id: str | None = None
    category: ObservationCategory = ObservationCategory.IMAGING
    code_system: str = ""
    code: str = ""
    code_display: str = ""
    value_quantity: float | None = None
    value_unit: str | None = None          # UCUM code
    value_string: str | None = None
    value_codeable_code: str | None = None
    value_codeable_system: str | None = None
    body_site_code: str | None = None      # SNOMED body structure
    laterality: Laterality | None = None
    status: ObservationStatus = ObservationStatus.FINAL
    effective_dt: datetime | None = None
    issued: datetime = field(default_factory=_utcnow)
    derived_from_device: str | None = None  # "{model_version}:{model_checksum}"
    source: str = ""                        # provenance for idempotent replacement


@dataclass
class ResultArtifact:
    name: str
    artifact_type: str  # "segmentation_nifti", "overlay", "report_json", etc.
    storage_path: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0


@dataclass
class AuditEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "default"
    action: AuditAction = AuditAction.STUDY_RECEIVED
    entity_type: str = ""
    entity_id: str = ""
    actor: str = "system"
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass
class RoutingRule:
    usecase_name: str
    body_parts: list[str] = field(default_factory=list)
    study_description_patterns: list[str] = field(default_factory=list)
    series_description_patterns: list[str] = field(default_factory=list)
    modality: str = "MR"
    priority: int = 0
    enabled: bool = True


@dataclass
class User:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    username: str = ""
    email: str = ""
    hashed_password: str = ""
    full_name: str = ""
    role: str = "viewer"
    tenant_id: str = "default"
    is_active: bool = True
    is_platform_admin: bool = False
    is_platform_operator: bool = False
    totp_enabled: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class Tenant:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    slug: str = ""
    is_active: bool = True
    status: str = "active"
    plan: str = "starter"
    features: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class TenantApiKey:
    """A DICOM-upload credential scoped to one tenant (see ``PendingStudyTenant`` — the
    mechanism that attributes an inbound study to the tenant that uploaded it)."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = ""
    name: str = ""
    key_hash: str = ""
    prefix: str = ""
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    is_active: bool = True
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_naive = self.expires_at.replace(tzinfo=None)
        return now_naive >= expires_naive


@dataclass
class PendingStudyTenant:
    """Attributes an inbound DICOM upload to a tenant before the study row exists.

    A self-authenticated upload (via a ``TenantApiKey``) arrives before ``StudyService``
    has ingested the study, so there is nowhere yet to stamp ``tenant_id``. This row is the
    bridge: registered at upload time, consumed once the Orthanc stable-study webhook fires
    and the real ``StudyRecord`` is created with this tenant_id.
    """

    study_instance_uid: str = ""
    tenant_id: str = ""
    api_key_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ModelVersion:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    usecase_name: str = ""
    version: str = ""
    storage_path: str = ""
    checksum: str = ""
    is_active: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ABExperiment:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    usecase_name: str = ""
    control_version: str = ""
    treatment_version: str = ""
    traffic_split: float = 0.5
    is_active: bool = True
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ABAssignment:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    experiment_id: str = ""
    study_instance_uid: str = ""
    assigned_version: str = ""
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class BatchUpload:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    total_items: int = 0
    completed_items: int = 0
    failed_items: int = 0
    status: str = "pending"
    created_by: str = ""
    tenant_id: str = "default"
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class BatchUploadItem:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    batch_id: str = ""
    study_instance_uid: str = ""
    status: str = "pending"
    error_detail: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class ReviewItem:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    study_instance_uid: str = ""
    usecase_name: str = ""
    result_id: str = ""
    confidence_score: float = 0.0
    status: str = "pending"
    reviewer: str | None = None
    review_notes: str = ""
    reviewed_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class AlertRule:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    event_type: str = ""
    condition: dict[str, Any] = field(default_factory=dict)
    webhook_url: str = ""
    is_active: bool = True
    tenant_id: str = "default"
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class AlertHistory:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    rule_id: str = ""
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "sent"
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class RetentionPolicy:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    entity_type: str = ""
    max_age_days: int = 365
    action: str = "archive"
    is_active: bool = True
    tenant_id: str = "default"
    created_at: datetime = field(default_factory=_utcnow)
