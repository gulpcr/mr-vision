"""Add share_links table for referring physician portal.

Revision ID: 013
Revises: 012
Create Date: 2026-05-20
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "share_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("study_instance_uid", sa.String(128), nullable=False),
        sa.Column("usecase_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("token", sa.String(128), nullable=False, unique=True),
        sa.Column("created_by", sa.String(128), server_default="system"),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_share_links_token", "share_links", ["token"])
    op.create_index("ix_share_links_result_id", "share_links", ["result_id"])


def downgrade():
    op.drop_index("ix_share_links_result_id", table_name="share_links")
    op.drop_index("ix_share_links_token", table_name="share_links")
    op.drop_table("share_links")
