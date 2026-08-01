from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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
    # The raw DICOM PatientID (0010,0020) — an *identifier* in the issuing hospital's
    # namespace (see settings.fhir_patient_identifier_system). Kept as-is; never
    # normalised in place, because it is the value the modality actually sent.
    patient_id = Column(String(64), nullable=True, index=True)
    # Resolved *reference* to the local patient row. patient_id above is matched to
    # patients.patient_ref by string equality, which has no referential integrity: a
    # stray prefix or a stripped leading zero silently orphans the study. This FK is
    # the durable link every exported resource's subject depends on. Populated by
    # OnboardingService when a study is linked (never by StudyRepository, so an
    # ingest-time refresh cannot clear an established link). SET NULL on patient
    # delete: losing the intake row must not cascade into imaging records.
    patient_record_id = Column(
        String(36), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_name = Column(String(256), nullable=True)
    patient_sex = Column(String(16), nullable=True)
    patient_age = Column(String(16), nullable=True)
    patient_weight_kg = Column(Float, nullable=True)
    patient_height_cm = Column(Float, nullable=True)
    study_date = Column(DateTime(timezone=True), nullable=True, index=True)
    # Precision of study_date: "second" when DICOM StudyTime (0008,0030) was present
    # and combined in, "date" when only StudyDate was available. Without this, a study
    # with no StudyTime is indistinguishable from one acquired exactly at midnight, so
    # consumers cannot tell a real time-of-day from a placeholder one.
    study_date_precision = Column(
        String(8), nullable=False, server_default="date"
    )
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
    assigned_at = Column(DateTime(timezone=True), nullable=True)
    reported_at = Column(DateTime(timezone=True), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_detail = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False, server_default="0")
    tenant_id = Column(String(36), nullable=True, server_default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    study = relationship("StudyRecord", back_populates="results")

    __table_args__ = (
        Index("ix_results_study_usecase_version", "study_instance_uid", "usecase_name", "version", unique=True),
        Index("ix_results_latest", "study_instance_uid", "usecase_name", postgresql_where=is_latest),
    )


class ObservationRecord(Base):
    """One row per measured clinical concept — the FHIR Observation shape.

    Before this table, clinical values had no addressable home: intake labs were
    free-text strings on ``orders``, AI measurements sat inside the opaque
    ``results_index.measurements`` JSON, and mammography findings were encoded in column
    *names* (``mass_right``). A JSON blob cannot serve
    ``Observation?patient=X&code=Y&date=ge…``, so none of that data was trendable or
    searchable — which is the entire purpose of the resource.

    Populated by ObservationService only (one writer), from intake orders, mammography
    reports, and — once per-plugin code maps exist — AI results. The legacy columns are
    dual-written for now so nothing that reads them breaks.
    """

    __tablename__ = "observations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), nullable=False, server_default="default", index=True)
    # patient_ref is the MRN — the FHIR search key, and what the composite index below
    # is built on. patient_id is the referential link (same distinction as
    # studies.patient_id vs studies.patient_record_id).
    patient_ref = Column(String(128), nullable=False, index=True)
    patient_id = Column(
        String(36), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    study_instance_uid = Column(
        String(128), ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    result_id = Column(
        String(36), ForeignKey("results_index.id", ondelete="SET NULL"), nullable=True
    )
    # vital-signs | laboratory | imaging | survey
    category = Column(String(32), nullable=False)
    code_system = Column(String(128), nullable=False)
    code = Column(String(64), nullable=False, index=True)
    code_display = Column(String(256), nullable=False, server_default="")
    # Exactly one value_* form is normally populated. A value_quantity is never stored
    # without value_unit (UCUM) — a quantity with no unit is not a measurement.
    value_quantity = Column(Float, nullable=True)
    value_unit = Column(String(32), nullable=True)
    value_string = Column(String(512), nullable=True)
    value_codeable_code = Column(String(64), nullable=True)
    value_codeable_system = Column(String(128), nullable=True)
    body_site_code = Column(String(64), nullable=True)
    laterality = Column(String(16), nullable=True)
    # FHIR Observation.status, mandatory 1..1.
    status = Column(String(16), nullable=False, server_default="final")
    effective_dt = Column(DateTime(timezone=True), nullable=True, index=True)
    issued = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # "{model_version}:{model_checksum}" — becomes Observation.device / Provenance.
    derived_from_device = Column(String(128), nullable=True)
    # Which writer produced the row ("order:{id}", "mammography:{study_uid}", ...), so a
    # re-save can replace exactly its own rows and nothing else.
    source = Column(String(128), nullable=False, server_default="", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # THE FHIR search index: Observation?patient=&code=&date= in one index scan,
        # which is precisely what a JSON blob cannot support.
        Index(
            "ix_observations_search",
            "tenant_id", "patient_ref", "code", "effective_dt",
        ),
        Index("ix_observations_study_code", "study_instance_uid", "code"),
        # These mirror migration 030 exactly. Declared here too so the ORM is a truthful
        # description of the table — otherwise a schema built from metadata (a test
        # fixture, a scratch database) silently lacks the invariants that production has.
        CheckConstraint(
            "value_quantity IS NULL OR value_unit IS NOT NULL",
            name="ck_observations_quantity_needs_unit",
        ),
        CheckConstraint(
            "category IN ('vital-signs', 'laboratory', 'imaging', 'survey')",
            name="ck_observations_category",
        ),
        CheckConstraint(
            "status IN ('registered', 'preliminary', 'final', 'amended', 'corrected', "
            "'cancelled', 'entered-in-error', 'unknown')",
            name="ck_observations_status",
        ),
        CheckConstraint(
            "laterality IS NULL OR laterality IN ('left', 'right', 'bilateral')",
            name="ck_observations_laterality",
        ),
    )


class ConditionRecord(Base):
    """A coded diagnosis / referral reason — the FHIR Condition shape.

    Before this table the only diagnosis data in the platform was prose:
    ``orders.indication`` (TEXT NOT NULL) and ``orders.clinical_history``. Free text
    cannot be coded, trended, or matched against a chart, so no Condition was derivable
    without NLP. The narrative is preserved in ``note`` and dual-written on the order;
    this table adds the coded, queryable form.

    ``study_instance_uid`` is ON DELETE SET NULL, deliberately unlike
    ``observations.study_instance_uid``: a diagnosis is a fact about the patient, so
    purging one imaging study must not erase it.
    """

    __tablename__ = "conditions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), nullable=False, server_default="default", index=True)
    # patient_ref (MRN) is the search key; patient_id is the referential link.
    patient_ref = Column(String(128), nullable=False, index=True)
    patient_id = Column(
        String(36), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    # ICD-10 or SNOMED CT — see app/domain/fhir_terminology.py for the system URIs.
    code_system = Column(String(128), nullable=False)
    code = Column(String(64), nullable=False, index=True)
    code_display = Column(String(512), nullable=False, server_default="")
    category = Column(String(32), nullable=False, server_default="encounter-diagnosis")
    clinical_status = Column(String(16), nullable=False, server_default="active")
    # Intake captures the *referrer's* stated reason, which this platform has not
    # verified — hence unconfirmed by default rather than confirmed.
    verification_status = Column(String(24), nullable=False, server_default="unconfirmed")
    onset_dt = Column(DateTime(timezone=True), nullable=True)
    recorded_dt = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # The original free-text indication / history, kept so nothing is lost in coding.
    note = Column(Text, nullable=True)
    study_instance_uid = Column(
        String(128),
        ForeignKey("studies.study_instance_uid", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_id = Column(String(36), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    # Which writer produced the row, so a re-save replaces exactly its own rows.
    source = Column(String(128), nullable=False, server_default="", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # Condition?patient=&code= — the search axes that matter for this resource.
        Index("ix_conditions_search", "tenant_id", "patient_ref", "code"),
        CheckConstraint(
            "category IN ('problem-list-item', 'encounter-diagnosis')",
            name="ck_conditions_category",
        ),
        CheckConstraint(
            "clinical_status IN ('active', 'recurrence', 'relapse', 'inactive', "
            "'remission', 'resolved')",
            name="ck_conditions_clinical_status",
        ),
        CheckConstraint(
            "verification_status IN ('unconfirmed', 'provisional', 'differential', "
            "'confirmed', 'refuted', 'entered-in-error')",
            name="ck_conditions_verification_status",
        ),
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
    registered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditLogRecord(Base):
    """The audit trail, shaped so it maps onto FHIR AuditEvent (step 8).

    The trail existed and was indexed, but ``actor`` was one untyped String(128) holding
    four different kinds of value depending on the call site — "system", "celery_worker",
    a user id, or a username. Given a row you could not tell which, so it could not be
    resolved to a Practitioner and could not be joined to ``users`` without guessing.
    The typed columns below fix that; ``actor`` is retained and still written so existing
    readers and the /api/audit filter keep working.
    """

    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Domain vocabulary (study_received, job_created, ...). In FHIR terms this is
    # AuditEvent.subtype, NOT AuditEvent.action — see action_crude.
    action = Column(String(64), nullable=False, index=True)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(256), nullable=False)
    # Legacy free-text actor. Kept for backwards compatibility; prefer actor_id.
    actor = Column(String(128), default="system")
    # ── AuditEvent.agent (step 8) ─────────────────────────────────────────────
    # practitioner | device | system — what kind of actor this was.
    actor_type = Column(String(16), nullable=True)
    # The stable id (users.id for a person), never a username: usernames change.
    actor_id = Column(String(128), nullable=True, index=True)
    actor_display = Column(String(256), nullable=True)
    # AuditEvent.action is a FIXED 5-code set: C(reate) R(ead) U(pdate) D(elete) E(xecute).
    # Recording it separately is what makes "show me every read of this record" answerable.
    action_crude = Column(String(1), nullable=True, index=True)
    # AuditEvent.outcome — 0 success, 4 minor failure, 8 serious, 12 major.
    outcome = Column(String(2), nullable=True)
    # AuditEvent.source.observer — which node produced the entry.
    source_observer = Column(String(128), nullable=True)
    # AuditEvent.agent.network.address — who the request came from.
    client_ip = Column(String(64), nullable=True)
    details = Column(JSON, default=dict)
    timestamp = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        # Answers "who accessed this patient's records, and when" — the query a HIPAA
        # access review actually asks, and which was previously unanswerable.
        Index("ix_audit_actor_time", "actor_id", "timestamp"),
        CheckConstraint(
            "action_crude IS NULL OR action_crude IN ('C', 'R', 'U', 'D', 'E')",
            name="ck_audit_action_crude",
        ),
    )


class UserRecord(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(128), unique=True, nullable=False, index=True)
    email = Column(String(256), unique=True, nullable=False)
    hashed_password = Column(String(512), nullable=False)
    full_name = Column(String(256), default="")
    # ── FHIR Practitioner (step 7) ────────────────────────────────────────────
    # The professional identifier — an NPI, state licence, or equivalent. This is the
    # identifier that actually distinguishes one clinician from another; a login account
    # alone is not a clinical actor. Namespaced by identifier_system, for the same reason
    # every other identifier is (see domain/fhir_terminology.py).
    identifier_system = Column(String(128), nullable=True)
    identifier_value = Column(String(128), nullable=True, index=True)
    # HumanName parts. full_name stays as the display form so nothing that reads it
    # breaks; these carry the structure FHIR requires.
    family_name = Column(String(128), nullable=True)
    given_names = Column(JSON, nullable=True)      # list[str] — given + middle
    name_prefix = Column(String(32), nullable=True)
    name_suffix = Column(String(32), nullable=True)
    # Primary role, retained as a denormalised cache so require_permission and every
    # existing RBAC path keep working unchanged. user_roles is the authoritative set.
    role = Column(String(32), nullable=False, default="viewer")
    tenant_id = Column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, default="default")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TenantRecord(Base):
    """A deployment tenant, and — once identified — the FHIR Organization behind it.

    A tenant was purely an isolation boundary: name + slug, with no way to say *which
    real-world organization* it is. That left identifiers unnamespaceable on export
    (Identifier.assigner has nothing to point at) and no Organization resource to act as
    DiagnosticReport.performer.
    """

    __tablename__ = "tenants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    slug = Column(String(128), unique=True, nullable=False, index=True)
    # ── FHIR Organization (step 7) ────────────────────────────────────────────
    identifier_system = Column(String(128), nullable=True)
    identifier_value = Column(String(128), nullable=True)
    # Organization.type, e.g. "prov" (healthcare provider) from
    # http://terminology.hl7.org/CodeSystem/organization-type
    org_type = Column(String(32), nullable=True)
    # Organization.address as a FHIR Address object; JSON because Address is structured
    # (line[], city, postalCode, country) and flattening it would repeat the
    # single-string mistake made with patient names.
    address = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PatientRecord(Base):
    """De-identified patient (intake). Identity stays in the DICOM/PACS layer;
    patient_ref is the MRN / DICOM PatientID used to match ingested studies."""

    __tablename__ = "patients"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_ref = Column(String(128), nullable=False)
    sex = Column(String(16), nullable=True)            # female | male | other
    age_band = Column(String(16), nullable=True)       # exact age in years (legacy rows may hold a band)
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    # The placer's own order number (HL7 ORC-2 / FHIR ServiceRequest.identifier), when
    # the order arrived from a RIS/EHR rather than being typed at reception. Namespaced
    # by settings.fhir_order_identifier_system. Indexed because inbound updates and
    # result delivery both look an order up by it.
    external_order_ref = Column(String(128), nullable=True, index=True)
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
    # When each sample was actually drawn. Without these, the derived Observation's
    # effectiveDateTime can only fall back to the intake time, which for a lab drawn days
    # earlier is simply the wrong date on a clinical result. Kept on the order (rather
    # than only on the Observation) so re-deriving is stable and idempotent.
    fasting_glucose_dt = Column(DateTime(timezone=True), nullable=True)
    creatinine_dt = Column(DateTime(timezone=True), nullable=True)
    study_instance_uid = Column(
        String(128), ForeignKey("studies.study_instance_uid", ondelete="SET NULL"), nullable=True
    )
    created_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_roles_tenant_name", "tenant_id", "name", unique=True),
    )


class UserRoleRecord(Base):
    """A practitioner's role at an organization, over a period — FHIR PractitionerRole.

    ``users.role`` is a single string, so a person could hold exactly one role. FHIR models
    a practitioner holding several PractitionerRoles across organizations and specialties,
    and — importantly for audit — bounded by a ``period``: to interpret a report signed
    two years ago you need to know what role the signer held *then*, not now.

    ``users.role`` is kept as the denormalised primary role so ``require_permission`` and
    every existing RBAC path are untouched; this table is additive and authoritative for
    the full set.
    """

    __tablename__ = "user_roles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Role name within the tenant, matching roles.name (same convention as users.role).
    role_name = Column(String(64), nullable=False)
    organization_id = Column(
        String(36), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    # PractitionerRole.code / .specialty — proprietary role names need mapping to
    # SNOMED or v2-0912 before export; unmapped roles simply are not exported.
    role_code_system = Column(String(128), nullable=True)
    role_code = Column(String(64), nullable=True)
    specialty_code_system = Column(String(128), nullable=True)
    specialty_code = Column(String(64), nullable=True)
    # PractitionerRole.period — NULL end means "still held".
    period_start = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=True)
    is_primary = Column(Boolean, nullable=False, server_default="false")
    tenant_id = Column(String(36), nullable=False, server_default="default", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_user_roles_user_role", "user_id", "role_name", unique=True),
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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ABAssignmentRecord(Base):
    __tablename__ = "ab_assignments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    experiment_id = Column(String(36), ForeignKey("ab_experiments.id", ondelete="CASCADE"), nullable=False)
    study_instance_uid = Column(String(128), nullable=False)
    assigned_version = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BatchUploadItemRecord(Base):
    __tablename__ = "batch_upload_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id = Column(String(36), ForeignKey("batch_uploads.id", ondelete="CASCADE"), nullable=False)
    study_instance_uid = Column(String(128), nullable=False)
    status = Column(String(32), default="pending")
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

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
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AlertRuleRecord(Base):
    __tablename__ = "alert_rules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    event_type = Column(String(64), nullable=False, index=True)
    condition = Column(JSON, default=dict)
    webhook_url = Column(String(1024), nullable=False)
    is_active = Column(Boolean, default=True)
    tenant_id = Column(String(36), default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AlertHistoryRecord(Base):
    __tablename__ = "alert_history"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rule_id = Column(String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(64), nullable=False)
    payload = Column(JSON, default=dict)
    status = Column(String(32), default="sent")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class RetentionPolicyRecord(Base):
    __tablename__ = "retention_policies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(256), nullable=False)
    entity_type = Column(String(64), nullable=False)
    max_age_days = Column(Integer, default=365)
    action = Column(String(32), default="archive")
    is_active = Column(Boolean, default=True)
    tenant_id = Column(String(36), default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ShareLinkRecord(Base):
    __tablename__ = "share_links"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    result_id = Column(String(36), nullable=False, index=True)
    study_instance_uid = Column(String(128), nullable=False)
    usecase_name = Column(String(128), nullable=False, default="")
    token = Column(String(128), nullable=False, unique=True, index=True)
    created_by = Column(String(128), default="system")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by = Column(String(128), nullable=True)
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    escalation_count = Column(Integer, default=0, nullable=False, server_default="0")
    tenant_id = Column(String(36), nullable=True, server_default="default")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

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
    # Structured per-breast finding slots. Pre-filled from the AI result (Mammo-CLIP
    # density + mass/calcification presence; radiologist-only slots default to their
    # negative) and editable by the radiologist. The free-text *_breast_findings above
    # are the narrative rendered FROM these slots. CHECK-constrained in migration 024:
    #   density in {a,b,c,d}; presence slots in {none, present}; nodes in {normal, abnormal}.
    density_right = Column(String(1), nullable=True)
    density_left = Column(String(1), nullable=True)
    mass_right = Column(String(16), nullable=True)
    mass_left = Column(String(16), nullable=True)
    calcification_right = Column(String(16), nullable=True)
    calcification_left = Column(String(16), nullable=True)
    skin_thickening_right = Column(String(16), nullable=True)
    skin_thickening_left = Column(String(16), nullable=True)
    nipple_retraction_right = Column(String(16), nullable=True)
    nipple_retraction_left = Column(String(16), nullable=True)
    architectural_distortion_right = Column(String(16), nullable=True)
    architectural_distortion_left = Column(String(16), nullable=True)
    axillary_nodes_right = Column(String(32), nullable=True)
    axillary_nodes_left = Column(String(32), nullable=True)
    reviewing_doctor = Column(String(256), nullable=True)
    reporting_doctor = Column(String(256), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tenant_id = Column(String(36), nullable=False, server_default="default")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
