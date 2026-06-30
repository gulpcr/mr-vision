"""Patient onboarding: de-identified patients + orders (intake), linked to studies.

Revision ID: 018
Revises: 017
Create Date: 2026-06-22

Implements the MedVision onboarding data model adapted to this codebase: studies
use a string `study_instance_uid` PK (PACS-ingested), so `orders.study_instance_uid`
links by that (ON DELETE SET NULL) rather than a UUID `study_id`. Patients are
de-identified (patient_ref = DICOM PatientID/MRN, coarse sex/age_band); identity
stays in the DICOM/PACS layer. Additive — nothing existing is altered.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("patient_ref", sa.String(128), nullable=False),
        sa.Column("sex", sa.String(16), nullable=True),
        sa.Column("age_band", sa.String(16), nullable=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sex IN ('female','male','other')", name="ck_patients_sex"),
        sa.CheckConstraint(
            "age_band IN ('0-17','18-39','40-64','65+')", name="ck_patients_age_band"
        ),
    )
    # patient_ref is unique per tenant (a tenant's MRN namespace).
    op.create_index("ix_patients_tenant_ref", "patients", ["tenant_id", "patient_ref"], unique=True)

    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("patient_id", sa.String(36), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("body_part", sa.String(64), nullable=False),
        sa.Column("referrer", sa.String(256), nullable=True),
        sa.Column("priority", sa.String(16), nullable=False, server_default="routine"),
        sa.Column("indication", sa.Text(), nullable=False),
        sa.Column("region_profile", sa.String(64), nullable=False),
        sa.Column("consent_ack", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("priority IN ('routine','stat')", name="ck_orders_priority"),
    )
    op.create_index("ix_orders_patient_id", "orders", ["patient_id"])
    op.create_index("ix_orders_study_uid", "orders", ["study_instance_uid"])


def downgrade() -> None:
    op.drop_index("ix_orders_study_uid", table_name="orders")
    op.drop_index("ix_orders_patient_id", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_patients_tenant_ref", table_name="patients")
    op.drop_table("patients")
