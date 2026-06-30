"""Add serum creatinine to orders for the report's TECHNIQUE/lab block.

Revision ID: 020
Revises: 019
Create Date: 2026-06-24

Adds `creatinine` to `orders`. BMI is intentionally NOT stored — it is derived
from height_cm/weight_kg wherever it is displayed. Additive/nullable.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("creatinine", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "creatinine")
