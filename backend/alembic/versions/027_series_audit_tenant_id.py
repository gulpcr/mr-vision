"""Tenant scoping for series and audit_log.

Revision ID: 027
Revises: 026
Create Date: 2026-07-07

Series had no tenant_id — it was only protected transitively via its parent
study. audit_log had none either, so /api/admin/audit leaked every tenant's
activity. Both get tenant_id now; series backfills from its parent study so
existing rows aren't orphaned into "default".
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "series",
        sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"),
    )
    op.execute(
        """
        UPDATE series
        SET tenant_id = studies.tenant_id
        FROM studies
        WHERE series.study_instance_uid = studies.study_instance_uid
        """
    )
    op.create_index("ix_series_tenant_id", "series", ["tenant_id"])

    op.add_column(
        "audit_log",
        sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_id", "audit_log")
    op.drop_column("audit_log", "tenant_id")
    op.drop_index("ix_series_tenant_id", "series")
    op.drop_column("series", "tenant_id")
