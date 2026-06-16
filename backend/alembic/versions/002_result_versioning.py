"""Add result versioning columns.

Revision ID: 002
Revises: 001
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add versioning columns
    op.add_column("results_index", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("results_index", sa.Column("is_latest", sa.Boolean(), nullable=False, server_default="true"))

    # Drop old unique index
    op.drop_index("ix_results_study_usecase", table_name="results_index")

    # Create new unique index on (study_uid, usecase, version)
    op.create_index(
        "ix_results_study_usecase_version",
        "results_index",
        ["study_instance_uid", "usecase_name", "version"],
        unique=True,
    )

    # Partial index for fast "latest" lookups
    op.create_index(
        "ix_results_latest",
        "results_index",
        ["study_instance_uid", "usecase_name"],
        postgresql_where=sa.text("is_latest = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_results_latest", table_name="results_index")
    op.drop_index("ix_results_study_usecase_version", table_name="results_index")

    # Re-create old unique index (only works if no duplicate study+usecase rows exist)
    op.create_index(
        "ix_results_study_usecase",
        "results_index",
        ["study_instance_uid", "usecase_name"],
        unique=True,
    )

    op.drop_column("results_index", "is_latest")
    op.drop_column("results_index", "version")
