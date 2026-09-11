"""Tenant workspace status, plan, and feature entitlements.

Revision ID: 034
Revises: 033
Create Date: 2026-08-19

Adds the columns TenantResolutionMiddleware and the feature-gate need: lifecycle
status (active/suspended/offboarded), subscription plan, and the per-tenant
enabled feature-key list (usecase names + add-ons like "cds", "longitudinal").
Existing tenants backfill to active / starter / no extra features.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
    )
    op.add_column(
        "tenants",
        sa.Column("plan", sa.String(32), nullable=False, server_default="starter"),
    )
    op.add_column(
        "tenants",
        sa.Column("features", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("tenants", "features")
    op.drop_column("tenants", "plan")
    op.drop_column("tenants", "status")
