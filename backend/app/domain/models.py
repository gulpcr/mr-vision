from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


from app.domain.enums import AuditAction, BodyPart, JobStatus, QAFlag


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Study:
    study_instance_uid: str
    patient_id: str | None = None
    patient_name: str | None = None
    patient_sex: str | None = None
    patient_age: str | None = None
    patient_weight_kg: float | None = None
    patient_height_cm: float | None = None
    study_date: datetime | None = None
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
class ResultArtifact:
    name: str
    artifact_type: str  # "segmentation_nifti", "overlay", "report_json", etc.
    storage_path: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0


@dataclass
class AuditEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
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
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass
class Tenant:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    slug: str = ""
    is_active: bool = True
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
