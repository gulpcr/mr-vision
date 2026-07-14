"""Tenant workspace status, plan, and feature entitlements.

Revision ID: 026
Revises: 025
Create Date: 2026-07-07

Adds the columns TenantResolutionMiddleware and the feature-gate need to move off
the hardcoded mock in infrastructure/tenant/repository.py: lifecycle status
(active/suspended/offboarded), subscription plan, and the per-tenant enabled
feature-key list (usecase names + add-ons like "cds", "longitudinal").
Existing tenants backfill to active / starter / no extra features.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "026"
down_revision = "025"
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
