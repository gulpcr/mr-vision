"""Batch upload tables.

Revision ID: 010
Revises: 009
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batch_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("total_items", sa.Integer(), default=0),
        sa.Column("completed_items", sa.Integer(), default=0),
        sa.Column("failed_items", sa.Integer(), default=0),
        sa.Column("status", sa.String(32), default="pending"),
        sa.Column("created_by", sa.String(128), default=""),
        sa.Column("tenant_id", sa.String(36), default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_batch_uploads_status", "batch_uploads", ["status"])

    op.create_table(
        "batch_upload_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("batch_uploads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("study_instance_uid", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), default="pending"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_batch_items_batch_id", "batch_upload_items", ["batch_id"])


def downgrade() -> None:
    op.drop_table("batch_upload_items")
    op.drop_table("batch_uploads")
