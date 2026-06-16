"""Model version registry.

Revision ID: 007
Revises: 006
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("usecase_name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("checksum", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), default=False),
        sa.Column("metadata", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_versions_usecase", "model_versions", ["usecase_name"])
    op.create_index("ix_model_versions_usecase_version", "model_versions", ["usecase_name", "version"], unique=True)


def downgrade() -> None:
    op.drop_table("model_versions")
