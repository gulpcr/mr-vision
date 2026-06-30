"""Bilateral mammography report (radiologist-authored, keyed by study).

Revision ID: 022
Revises: 021
Create Date: 2026-06-29

One editable mammography report row per study, pre-filled from the mammography AI
result and finalised by the radiologist. Additive — nothing existing is altered.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None

_BIRADS = "('0','1','2','3','4','5','6')"


def upgrade() -> None:
    op.create_table(
        "mammography_reports",
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("file_no", sa.String(64), nullable=True),
        sa.Column("status", sa.String(64), nullable=True),
        sa.Column("contact", sa.String(64), nullable=True),
        sa.Column("procedure", sa.Text(), nullable=True),
        sa.Column("clinical_features", sa.Text(), nullable=True),
        sa.Column("right_breast_findings", sa.Text(), nullable=True),
        sa.Column("left_breast_findings", sa.Text(), nullable=True),
        sa.Column("opinion", sa.Text(), nullable=True),
        sa.Column("birads_right", sa.String(8), nullable=True),
        sa.Column("birads_left", sa.String(8), nullable=True),
        sa.Column("reviewing_doctor", sa.String(256), nullable=True),
        sa.Column("reporting_doctor", sa.String(256), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            f"birads_right IS NULL OR birads_right IN {_BIRADS}", name="ck_mammo_birads_right"
        ),
        sa.CheckConstraint(
            f"birads_left IS NULL OR birads_left IN {_BIRADS}", name="ck_mammo_birads_left"
        ),
    )


def downgrade() -> None:
    op.drop_table("mammography_reports")
