"""Add richer clinical fields to orders so the PET-CT report is fully populated.

Revision ID: 019
Revises: 018
Create Date: 2026-06-22

Adds clinical_history, comparative_study, height_cm, weight_kg, fasting_glucose,
injection_site to `orders` — these fill the report's CLINICAL HISTORY / TECHNIQUE
(height, weight, fasting blood sugar, site of injection) / COMPARATIVE STUDY
fields. All nullable/additive.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("clinical_history", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("comparative_study", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("height_cm", sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("weight_kg", sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("fasting_glucose", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("injection_site", sa.String(128), nullable=True))


def downgrade() -> None:
    for col in ("injection_site", "fasting_glucose", "weight_kg", "height_cm",
                "comparative_study", "clinical_history"):
        op.drop_column("orders", col)
