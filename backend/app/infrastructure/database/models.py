from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class StudyRecord(Base):
    __tablename__ = "studies"

    study_instance_uid = Column(String(128), primary_key=True)
    patient_id = Column(String(64), nullable=True, index=True)
    patient_name = Column(String(256), nullable=True)
    patient_sex = Column(String(16), nullable=True)
    patient_age = Column(String(16), nullable=True)
    patient_weight_kg = Column(Float, nullable=True)
    patient_height_cm = Column(Float, nullable=True)
    study_date = Column(DateTime, nullable=True, index=True)
    study_description = Column(String(512), nullable=True)
    accession_number = Column(String(64), nullable=True, index=True)
    referring_physician = Column(String(256), nullable=True)
    body_part_examined = Column(String(64), nullable=True, index=True)
    modality = Column(String(16), nullable=True, index=True)
    institution_name = Column(String(256), nullable=True)
    orthanc_id = Column(String(128), nullable=True)
    tenant_id = Column(String(36), nullable=True, server_default="default")
    # ── Reading workflow (radiologist lifecycle) ──────────────────────────────
    # unread → in_progress → reported → signed. created_at is the "received" time
    # used for turnaround-time tracking.
    reading_status = Column(String(16), nullable=False, server_default="unread", index=True)
    assigned_to = Column(String(36), nullable=True, index=True)          # user id
    assigned_to_username = Column(String(128), nullable=True)            # denormalised for display
    assigned_at = Column(DateTime, nullable=True)
    reported_at = Column(DateTime, nullable=True)
    signed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    series = relationship("SeriesRecord", back_populates="study", cascade="all, delete-orphan")
    job_runs = relationship("JobRunRecord", back_populates="study", cascade="all, delete-orphan")
    results = relationship("ResultRecord", back_populates="study", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_studies_body_modality", "body_part_examined", "modality"),
    )


class SeriesRecord(Base):
    __tablename__ = "series"

    series_instance_uid = Column(String(128), primary_key=True)
    study_instance_uid = Column(
        String(128), ForeignKey("studies.study_instance_uid", ondelete="CASCADE"), nullable=False
    )
    series_number = Column(Integer, nullable=True)
    series_description = Column(String(512), nullable=True)
    modality = Column(String(16), nullable=True)
    body_part_examined = Column(String(64), nullable=True)
    protocol_name = Column(String(256), nullable=True)
    num_instances = Column(Integer, default=0)
    slice_thickness = Column(Float, nullable=True)
    pixel_spacing = Column(JSON, nullable=True)
    image_orientation = Column(String(256), nullable=True)
    orthanc_id = Column(String(128), nullable=True)
    dicom_tags = Column(JSON, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    study = relationship("StudyRecord", back_populates="series")

    __table_args__ = (
        Index("ix_series_study_uid", "study_instance_uid"),
    )


class JobRunRecord(Base):
    __tablename__ = "job_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    study_instance_uid = Column(
        String(128), ForeignKey("studies.study_instance_uid", ondelete="CASCADE"), nullable=False
    )
    usecase_name = Column(String(128), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    priority = Column(Integer, default=0)
    progress = Column(Float, default=0.0)
    status_message = Column(Text, default="")
    worker_id = Column(String(128), nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_detail = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False, server_default="0")
    tenant_id = Column(String(36), nullable=True, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    study = relationship("StudyRecord", back_populates="job_runs")

    __table_args__ = (
        Index("ix_job_runs_study_usecase", "study_instance_uid", "usecase_name"),
        Index("ix_job_runs_status_created", "status", "created_at"),
    )


class ResultRecord(Base):
    __tablename__ = "results_index"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    study_instance_uid = Column(
        String(128), ForeignKey("studies.study_instance_uid", ondelete="CASCADE"), nullable=False
    )
    usecase_name = Column(String(128), nullable=False, index=True)
    job_id = Column(String(36), ForeignKey("job_runs.id", ondelete="SET NULL"), nullable=True)
    summary = Column(JSON, default=dict)
    measurements = Column(JSON, default=dict)
    qa_flags = Column(JSON, default=list)
    qa_details = Column(JSON, default=dict)
    model_version = Column(String(64), nullable=False)
    model_checksum = Column(String(128), nullable=False)
    artifacts = Column(JSON, default=list)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    is_latest = Column(Boolean, nullable=False, default=True, server_default="true")
    tenant_id = Column(String(36), nullable=True, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    study = relationship("StudyRecord", back_populates="results")

    __table_args__ = (
        Index("ix_results_study_usecase_version", "study_instance_uid", "usecase_name", "version", unique=True),
        Index("ix_results_latest", "study_instance_uid", "usecase_name", postgresql_where=is_latest),
    )


class UseCaseRegistryRecord(Base):
    __tablename__ = "usecase_registry"

    name = Column(String(128), primary_key=True)
    version = Column(String(32), nullable=False)
    supported_body_parts = Column(JSON, default=list)
    required_sequences = Column(JSON, default=list)
    model_type = Column(String(64), nullable=False)
    enabled = Column(Boolean, default=True)
    module_path = Column(String(512), nullable=False)
    description = Column(Text, default="")
    ensemble_config = Column(JSON, nullable=True)
    registered_at = Column(DateTime, server_default=func.now(), nullable=False)


class AuditLogRecord(Base):
    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action = Column(String(64), nullable=False, index=True)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(256), nullable=False)
    actor = Column(String(128), default="system")
    details = Column(JSON, default=dict)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )


class UserRecord(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(128), unique=True, nullable=False, index=True)
    email = Column(String(256), unique=True, nullable=False)
    hashed_password = Column(String(512), nullable=False)
    full_name = Column(String(256), default="")
    role = Column(String(32), nullable=False, default="viewer")
    tenant_id = Column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, default="default")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class TenantRecord(Base):
    __tablename__ = "tenants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    slug = Column(String(128), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class PatientRecord(Base):
    """De-identified patient (intake). Identity stays in the DICOM/PACS layer;
    patient_ref is the MRN / DICOM PatientID used to match ingested studies."""

    __tablename__ = "patients"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_ref = Column(String(128), nullable=False)
    sex = Column(String(16), nullable=True)            # female | male | other
    age_band = Column(String(16), nullable=True)       # 0-17 | 18-39 | 40-64 | 65+
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_patients_tenant_ref", "tenant_id", "patient_ref", unique=True),
    )


class OrderRecord(Base):
    """Imaging order (intake) linking a patient's clinical data to a study."""

    __tablename__ = "orders"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id = Column(String(36), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    modality = Column(String(16), nullable=False)
    body_part = Column(String(64), nullable=False)
    referrer = Column(String(256), nullable=True)
    priority = Column(String(16), nullable=False, server_default="routine")  # routine | stat
    indication = Column(Text, nullable=False)
    region_profile = Column(String(64), nullable=False)
    consent_ack = Column(Boolean, nullable=False, default=False, server_default="false")
    # Richer clinical fields that populate the PET-CT report.
    clinical_history = Column(Text, nullable=True)
    comparative_study = Column(Text, nullable=True)
    height_cm = Column(Float, nullable=True)
    weight_kg = Column(Float, nullable=True)
    fasting_glucose = Column(String(32), nullable=True)
    injection_site = Column(String(128), nullable=True)
    creatinine = Column(String(32), nullable=True)
    study_instance_uid = Column(
        String(128), ForeignKey("studies.study_instance_uid", ondelete="SET NULL"), nullable=True
    )
    created_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_orders_patient_id", "patient_id"),
        Index("ix_orders_study_uid", "study_instance_uid"),
    )


class RoleRecord(Base):
    """Per-tenant RBAC role with a permission set (see app.domain.permissions).

    System roles (is_system=True) are seeded and cannot be deleted; their
    permissions may be edited. Custom roles can be created/edited/deleted.
    ``users.role`` references a role by ``name`` within the same tenant.
    """

    __tablename__ = "roles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), nullable=False, server_default="default", index=True)
    name = Column(String(64), nullable=False)
    permissions = Column(JSON, nullable=False, default=list)
    is_system = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_roles_tenant_name", "tenant_id", "name", unique=True),
    )


class ModelVersionRecord(Base):
    __tablename__ = "model_versions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    usecase_name = Column(String(128), nullable=False, index=True)
    version = Column(String(64), nullable=False)
    storage_path = Column(String(512), nullable=False)
    checksum = Column(String(256), nullable=False)
    is_active = Column(Boolean, default=False)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_model_versions_usecase_version", "usecase_name", "version", unique=True),
    )


class ABExperimentRecord(Base):
    __tablename__ = "ab_experiments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    usecase_name = Column(String(128), nullable=False, index=True)
    control_version = Column(String(64), nullable=False)
    treatment_version = Column(String(64), nullable=False)
    traffic_split = Column(Float, default=0.5)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ABAssignmentRecord(Base):
    __tablename__ = "ab_assignments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    experiment_id = Column(String(36), ForeignKey("ab_experiments.id", ondelete="CASCADE"), nullable=False)
    study_instance_uid = Column(String(128), nullable=False)
    assigned_version = Column(String(64), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_ab_assignments_experiment_study", "experiment_id", "study_instance_uid", unique=True),
    )


class BatchUploadRecord(Base):
    __tablename__ = "batch_uploads"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    total_items = Column(Integer, default=0)
    completed_items = Column(Integer, default=0)
    failed_items = Column(Integer, default=0)
    status = Column(String(32), default="pending", index=True)
    created_by = Column(String(128), default="")
    tenant_id = Column(String(36), default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class BatchUploadItemRecord(Base):
    __tablename__ = "batch_upload_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id = Column(String(36), ForeignKey("batch_uploads.id", ondelete="CASCADE"), nullable=False)
    study_instance_uid = Column(String(128), nullable=False)
    status = Column(String(32), default="pending")
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_batch_items_batch_id", "batch_id"),
    )


class ReviewQueueRecord(Base):
    __tablename__ = "review_queue"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    study_instance_uid = Column(String(128), nullable=False, index=True)
    usecase_name = Column(String(128), nullable=False)
    result_id = Column(String(36), nullable=False)
    confidence_score = Column(Float, default=0.0)
    status = Column(String(32), default="pending", index=True)
    reviewer = Column(String(128), nullable=True)
    review_notes = Column(Text, default="")
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class AlertRuleRecord(Base):
    __tablename__ = "alert_rules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    event_type = Column(String(64), nullable=False, index=True)
    condition = Column(JSON, default=dict)
    webhook_url = Column(String(1024), nullable=False)
    is_active = Column(Boolean, default=True)
    tenant_id = Column(String(36), default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class AlertHistoryRecord(Base):
    __tablename__ = "alert_history"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rule_id = Column(String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(64), nullable=False)
    payload = Column(JSON, default=dict)
    status = Column(String(32), default="sent")
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)


class RetentionPolicyRecord(Base):
    __tablename__ = "retention_policies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    entity_type = Column(String(64), nullable=False)
    max_age_days = Column(Integer, default=365)
    action = Column(String(32), default="archive")
    is_active = Column(Boolean, default=True)
    tenant_id = Column(String(36), default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ShareLinkRecord(Base):
    __tablename__ = "share_links"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    result_id = Column(String(36), nullable=False, index=True)
    study_instance_uid = Column(String(128), nullable=False)
    usecase_name = Column(String(128), nullable=False, default="")
    token = Column(String(128), nullable=False, unique=True, index=True)
    created_by = Column(String(128), default="system")
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class CriticalAlertRecord(Base):
    __tablename__ = "critical_alerts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    study_instance_uid = Column(String(128), nullable=False, index=True)
    usecase_name = Column(String(128), nullable=False, index=True)
    result_id = Column(String(36), nullable=False, index=True)
    patient_id = Column(String(64), nullable=True, index=True)
    finding_type = Column(String(128), nullable=False)
    severity = Column(String(16), nullable=False)          # CRITICAL | WARNING
    title = Column(String(512), nullable=False)
    message = Column(Text, nullable=False)
    details = Column(JSON, default=dict)
    status = Column(String(32), nullable=False, default="pending", index=True)
    notification_channels = Column(JSON, default=list)     # ["websocket", "email", "webhook"]
    acknowledged_at = Column(DateTime, nullable=True)
    acknowledged_by = Column(String(128), nullable=True)
    escalated_at = Column(DateTime, nullable=True)
    escalation_count = Column(Integer, default=0, nullable=False, server_default="0")
    tenant_id = Column(String(36), nullable=True, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_critical_alerts_status_severity", "status", "severity"),
        Index("ix_critical_alerts_study_usecase", "study_instance_uid", "usecase_name"),
    )


class MammographyReportRecord(Base):
    """Radiologist-authored bilateral mammography report, keyed by study.

    Pre-filled from the mammography AI result (per-breast findings / BI-RADS) and
    then edited/finalised by the radiologist. One row per study (upsert)."""

    __tablename__ = "mammography_reports"

    study_instance_uid = Column(
        String(128),
        ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
        primary_key=True,
    )
    # bilateral | right | left — which breast(s) the study imaged.
    laterality = Column(String(16), nullable=True)
    # Header fields not derivable from DICOM/study data.
    file_no = Column(String(64), nullable=True)
    status = Column(String(64), nullable=True)
    contact = Column(String(64), nullable=True)
    # Report body (free text).
    procedure = Column(Text, nullable=True)
    clinical_features = Column(Text, nullable=True)
    right_breast_findings = Column(Text, nullable=True)
    left_breast_findings = Column(Text, nullable=True)
    opinion = Column(Text, nullable=True)
    # BI-RADS 0-6 per breast (stored as string; CHECK-constrained in the migration).
    birads_right = Column(String(8), nullable=True)
    birads_left = Column(String(8), nullable=True)
    reviewing_doctor = Column(String(256), nullable=True)
    reporting_doctor = Column(String(256), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MriReportRecord(Base):
    """Radiologist-authored MRI narrative report, keyed by study.

    Follows the standard radiology narrative layout (EXAMINATION / TECHNIQUE /
    CLINICAL INDICATION / FINDINGS / IMPRESSION). Optionally pre-filled from the
    MRI AI result and then edited/finalised by the radiologist. Surfaced for the
    MRI use cases (brain/spine/chest/abdomen). One row per study (upsert)."""

    __tablename__ = "mri_reports"

    study_instance_uid = Column(
        String(128),
        ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
        primary_key=True,
    )
    # Report body (free text).
    examination = Column(Text, nullable=True)
    technique = Column(Text, nullable=True)
    clinical_indication = Column(Text, nullable=True)
    findings = Column(Text, nullable=True)
    impression = Column(Text, nullable=True)
    # Signatory (config-defaulted, editable per report).
    reporting_doctor = Column(String(256), nullable=True)
    doctor_title = Column(String(256), nullable=True)
    doctor_qualifications = Column(String(256), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
