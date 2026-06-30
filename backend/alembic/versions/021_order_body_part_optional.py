"""Make orders.body_part nullable — intake now captures a single study/modality
type (e.g. "Brain MRI", "PET-CT", "Mammogram") instead of a separate body part.

Revision ID: 021
Revises: 020
Create Date: 2026-06-24

The exam-type selection is stored in `orders.modality`; `body_part` becomes an
optional/derived field. Routing is unaffected (it runs off the DICOM StudyRecord,
not the order). Existing rows keep their value.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("orders", "body_part", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    # Backfill any NULLs before restoring the NOT NULL constraint.
    op.execute("UPDATE orders SET body_part = '' WHERE body_part IS NULL")
    op.alter_column("orders", "body_part", existing_type=sa.String(64), nullable=False)
