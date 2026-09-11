"""Tenant API keys for scoped external access (DICOM upload) + pending study->tenant map.

Revision ID: 036
Revises: 035
Create Date: 2026-08-19

tenant_api_keys: hashed, scoped, revocable keys handed to a tenant's external
systems (e.g. their DICOM sender) — distinct from the single global admin
api_key in Settings, which grants unscoped platform access.

pending_study_tenants: DICOM images enter via Orthanc (DICOM protocol / REST),
which has no concept of tenant. When the upload gateway pushes an instance into
Orthanc it registers study_instance_uid -> tenant_id here; Orthanc's async
notify-stable-study webhook (which only knows the UID) consults this row to
stamp the right tenant when the Study record is first created, then the row is
consumed (deleted).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_tenant_api_keys_hash_active", "tenant_api_keys", ["key_hash"],
        unique=True, postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_tenant_api_keys_tenant_id", "tenant_api_keys", ["tenant_id"])

    op.create_table(
        "pending_study_tenants",
        sa.Column("study_instance_uid", sa.String(128), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "api_key_id", sa.String(36),
            sa.ForeignKey("tenant_api_keys.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_pending_study_tenants_tenant_id", "pending_study_tenants", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_table("pending_study_tenants")
    op.drop_index("ix_tenant_api_keys_tenant_id", "tenant_api_keys")
    op.drop_index("ix_tenant_api_keys_hash_active", "tenant_api_keys")
    op.drop_table("tenant_api_keys")
