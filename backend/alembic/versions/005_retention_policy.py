"""Retention policies table.

Revision ID: 005
Revises: 004
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retention_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("max_age_days", sa.Integer(), default=365),
        sa.Column("action", sa.String(32), default="archive"),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("tenant_id", sa.String(36), default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("retention_policies")
