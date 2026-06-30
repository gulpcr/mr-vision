"""Add laterality to mammography_reports (bilateral / right / left).

Revision ID: 023
Revises: 022
Create Date: 2026-06-30

Mammography is one MG modality; laterality is a property of the study (which
breast(s) were imaged). Stored on the report so the rendered title/sections and
PDF adapt (BILATERAL / RIGHT / LEFT MAMMOGRAPHY) without re-deriving.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mammography_reports",
        sa.Column("laterality", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mammography_reports", "laterality")
