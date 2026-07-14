"""Tenant scoping for review_queue and share_links.

Revision ID: 031
Revises: 030
Create Date: 2026-07-09

Closes the last two tables from the tenant-isolation audit that had no
tenant_id at all: review_queue (radiologist review of actual results — real
patient-adjacent data) and share_links (external report-sharing tokens).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_queue",
        sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"),
    )
    op.create_index("ix_review_queue_tenant_id", "review_queue", ["tenant_id"])

    op.add_column(
        "share_links",
        sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"),
    )
    op.create_index("ix_share_links_tenant_id", "share_links", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_share_links_tenant_id", "share_links")
    op.drop_column("share_links", "tenant_id")
    op.drop_index("ix_review_queue_tenant_id", "review_queue")
    op.drop_column("review_queue", "tenant_id")
