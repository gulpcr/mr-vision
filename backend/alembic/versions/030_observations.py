"""The observations table — row-per-value clinical data (FHIR Observation).

Revision ID: 030
Revises: 029
Create Date: 2026-07-29

FHIR_R4_COMPLIANCE_AUDIT.md step 4, the structural change that unblocks compliance
pillars 2 (terminology) and 3 (serialisation/search) together.

Clinical values had no addressable home in this schema:

* intake labs were free-text strings — ``orders.fasting_glucose`` and
  ``orders.creatinine`` are String(32), so "5.4", "5.4 mg/dl" and "normal" were all
  equally valid, with no unit, no draw time, no status and no code;
* vitals were bare floats on two different parent rows (``orders.weight_kg`` and
  ``studies.patient_weight_kg``) with no measurement time;
* AI measurements sat inside the opaque ``results_index.measurements`` JSON, where
  clinical numbers (SUVmax) are structurally indistinguishable from technical metadata
  (``image_dimensions``);
* mammography findings encoded laterality in column *names* (``mass_right``).

None of those can answer ``Observation?patient=X&code=Y&date=ge…``. That query is the
reason the resource exists, and it needs one row per measured concept plus an index — the
``ix_observations_search`` composite below, which turns the canonical FHIR Observation
search into a single index scan.

Additive: no existing column is altered or dropped. The legacy ``orders`` columns stay
and are dual-written, so every current reader (intake UI, PET-CT PDF) keeps working;
they can be deprecated once those readers move over.

Backfill is intentionally NOT done here. Existing intake strings are ambiguous
("normal", "<0.5", a value with no unit and no draw date) and ObservationService applies
real parsing rules with a documented refusal path for what it cannot interpret. Running
that logic through a raw SQL migration would either duplicate it or guess. Existing rows
become observations the next time their order is saved.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def _order_columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("orders")}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    # Draw/measurement timestamps for the two intake labs. Without them a derived
    # Observation's effectiveDateTime can only fall back to the intake time, which for a
    # sample drawn days earlier is the wrong date on a clinical result. Held on the order
    # so re-deriving the Observation is stable rather than resetting the date each save.
    if "orders" in tables:
        present = _order_columns(bind)
        for column in ("fasting_glucose_dt", "creatinine_dt"):
            if column not in present:
                op.add_column(
                    "orders", sa.Column(column, sa.DateTime(timezone=True), nullable=True)
                )

    if "observations" in tables:
        return

    op.create_table(
        "observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        # patient_ref (MRN) is the FHIR search key; patient_id is the referential link.
        sa.Column("patient_ref", sa.String(128), nullable=False),
        sa.Column(
            "patient_id",
            sa.String(36),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "result_id",
            sa.String(36),
            sa.ForeignKey("results_index.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("code_system", sa.String(128), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("code_display", sa.String(256), nullable=False, server_default=""),
        sa.Column("value_quantity", sa.Float, nullable=True),
        sa.Column("value_unit", sa.String(32), nullable=True),
        sa.Column("value_string", sa.String(512), nullable=True),
        sa.Column("value_codeable_code", sa.String(64), nullable=True),
        sa.Column("value_codeable_system", sa.String(128), nullable=True),
        sa.Column("body_site_code", sa.String(64), nullable=True),
        sa.Column("laterality", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="final"),
        sa.Column("effective_dt", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "issued", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("derived_from_device", sa.String(128), nullable=True),
        sa.Column("source", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # A quantity with no unit is not a measurement — reject it at the database level
        # rather than discovering it downstream as `unit: "unknown"`.
        sa.CheckConstraint(
            "value_quantity IS NULL OR value_unit IS NOT NULL",
            name="ck_observations_quantity_needs_unit",
        ),
        sa.CheckConstraint(
            "category IN ('vital-signs', 'laboratory', 'imaging', 'survey')",
            name="ck_observations_category",
        ),
        sa.CheckConstraint(
            "status IN ('registered', 'preliminary', 'final', 'amended', 'corrected', "
            "'cancelled', 'entered-in-error', 'unknown')",
            name="ck_observations_status",
        ),
        sa.CheckConstraint(
            "laterality IS NULL OR laterality IN ('left', 'right', 'bilateral')",
            name="ck_observations_laterality",
        ),
    )

    op.create_index("ix_observations_tenant_id", "observations", ["tenant_id"])
    op.create_index("ix_observations_patient_ref", "observations", ["patient_ref"])
    op.create_index("ix_observations_study_uid", "observations", ["study_instance_uid"])
    op.create_index("ix_observations_code", "observations", ["code"])
    op.create_index("ix_observations_effective_dt", "observations", ["effective_dt"])
    op.create_index("ix_observations_source", "observations", ["source"])
    # The canonical FHIR Observation search: patient + code + date, one index scan.
    op.create_index(
        "ix_observations_search",
        "observations",
        ["tenant_id", "patient_ref", "code", "effective_dt"],
    )
    op.create_index("ix_observations_study_code", "observations", ["study_instance_uid", "code"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "observations" in tables:
        # Derived data only — every row is regenerated from its source on the next save.
        op.drop_table("observations")

    if "orders" in tables:
        present = _order_columns(bind)
        for column in ("creatinine_dt", "fasting_glucose_dt"):
            if column in present:
                op.drop_column("orders", column)
