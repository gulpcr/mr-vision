"""MRI narrative report (radiologist-authored, keyed by study).

Revision ID: 024
Revises: 023
Create Date: 2026-06-30

One editable MRI report row per study, following the standard radiology narrative
layout (EXAMINATION / TECHNIQUE / CLINICAL INDICATION / FINDINGS / IMPRESSION) and
finalised by the radiologist. Surfaced for the MRI use cases (brain/spine/chest/
abdomen). Additive — nothing existing is altered.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mri_reports",
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("examination", sa.Text(), nullable=True),
        sa.Column("technique", sa.Text(), nullable=True),
        sa.Column("clinical_indication", sa.Text(), nullable=True),
        sa.Column("findings", sa.Text(), nullable=True),
        sa.Column("impression", sa.Text(), nullable=True),
        sa.Column("reporting_doctor", sa.String(256), nullable=True),
        sa.Column("doctor_title", sa.String(256), nullable=True),
        sa.Column("doctor_qualifications", sa.String(256), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mri_reports")
