"""A/B testing tables.

Revision ID: 009
Revises: 008
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ab_experiments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("usecase_name", sa.String(128), nullable=False),
        sa.Column("control_version", sa.String(64), nullable=False),
        sa.Column("treatment_version", sa.String(64), nullable=False),
        sa.Column("traffic_split", sa.Float(), default=0.5),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ab_experiments_usecase", "ab_experiments", ["usecase_name"])

    op.create_table(
        "ab_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("experiment_id", sa.String(36), sa.ForeignKey("ab_experiments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("study_instance_uid", sa.String(128), nullable=False),
        sa.Column("assigned_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ab_assignments_experiment_study", "ab_assignments", ["experiment_id", "study_instance_uid"], unique=True)


def downgrade() -> None:
    op.drop_table("ab_assignments")
    op.drop_table("ab_experiments")
