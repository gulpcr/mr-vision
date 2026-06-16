"""Active learning review queue.

Revision ID: 011
Revises: 010
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_queue",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False),
        sa.Column("usecase_name", sa.String(128), nullable=False),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("confidence_score", sa.Float(), default=0.0),
        sa.Column("status", sa.String(32), default="pending"),
        sa.Column("reviewer", sa.String(128), nullable=True),
        sa.Column("review_notes", sa.Text(), default=""),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_review_queue_status", "review_queue", ["status"])
    op.create_index("ix_review_queue_study", "review_queue", ["study_instance_uid"])


def downgrade() -> None:
    op.drop_table("review_queue")
