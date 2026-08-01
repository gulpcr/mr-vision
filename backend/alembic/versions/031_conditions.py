"""The conditions table — coded diagnoses / referral reasons (FHIR Condition).

Revision ID: 031
Revises: 030
Create Date: 2026-07-29

FHIR_R4_COMPLIANCE_AUDIT.md step 6.

The only diagnosis data in this platform was prose: ``orders.indication`` (TEXT NOT NULL)
and ``orders.clinical_history``. There was no ICD-10 or SNOMED anywhere, so no
``Condition`` resource was derivable without NLP, and nothing could be matched against a
hospital's problem list or trended over time.

This adds the coded form alongside the narrative — the free text is preserved in
``conditions.note`` and still dual-written on the order, so every existing reader keeps
working.

Two deliberate design points:

* ``study_instance_uid`` is **ON DELETE SET NULL**, unlike
  ``observations.study_instance_uid`` which cascades. A diagnosis is a fact about the
  patient; purging one imaging study for retention must not erase it. (Cascading derived
  *imaging measurements* with their study is correct; cascading a diagnosis is not.)
* ``verification_status`` defaults to ``unconfirmed``. Intake records the *referrer's*
  stated reason for the scan, which this platform has not established — asserting
  ``confirmed`` would overstate what we know.

**No MedicationRequest table**, deliberately. This platform never authors prescriptions:
it has no drug database, no dosing, and no medication workflow. Treatment-timeline data
exists only to be *read* from the hospital's EHR (FHIR_INTEGRATION_PLAN.md §3 Tier 2,
where chemo/immunotherapy timing prevents reading pseudoprogression as progression), and
§4.2 requires fetched clinical data to be held transiently rather than persisted. A table
nothing writes to is dead schema; when EHR integration lands, medications arrive as
transient context, not rows here.

Additive: nothing existing is altered or dropped.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "conditions" in tables:
        return

    op.create_table(
        "conditions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("patient_ref", sa.String(128), nullable=False),
        sa.Column(
            "patient_id",
            sa.String(36),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("code_system", sa.String(128), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("code_display", sa.String(512), nullable=False, server_default=""),
        sa.Column(
            "category", sa.String(32), nullable=False, server_default="encounter-diagnosis"
        ),
        sa.Column("clinical_status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "verification_status", sa.String(24), nullable=False, server_default="unconfirmed"
        ),
        sa.Column("onset_dt", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_dt", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "category IN ('problem-list-item', 'encounter-diagnosis')",
            name="ck_conditions_category",
        ),
        sa.CheckConstraint(
            "clinical_status IN ('active', 'recurrence', 'relapse', 'inactive', "
            "'remission', 'resolved')",
            name="ck_conditions_clinical_status",
        ),
        sa.CheckConstraint(
            "verification_status IN ('unconfirmed', 'provisional', 'differential', "
            "'confirmed', 'refuted', 'entered-in-error')",
            name="ck_conditions_verification_status",
        ),
    )
    op.create_index("ix_conditions_tenant_id", "conditions", ["tenant_id"])
    op.create_index("ix_conditions_patient_ref", "conditions", ["patient_ref"])
    op.create_index("ix_conditions_code", "conditions", ["code"])
    op.create_index("ix_conditions_study_uid", "conditions", ["study_instance_uid"])
    op.create_index("ix_conditions_source", "conditions", ["source"])
    # Condition?patient=&code=
    op.create_index("ix_conditions_search", "conditions", ["tenant_id", "patient_ref", "code"])


def downgrade() -> None:
    bind = op.get_bind()
    if "conditions" not in set(sa.inspect(bind).get_table_names()):
        return
    # Derived/coded companion data; the narrative indication remains on orders.
    op.drop_table("conditions")
