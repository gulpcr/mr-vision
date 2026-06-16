"""Add retry_count to job_runs.

Revision ID: 003
Revises: 002
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_runs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("job_runs", "retry_count")
