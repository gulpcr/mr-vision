"""Plan-level feature defaults.

Revision ID: 029
Revises: 028
Create Date: 2026-07-08

Presence-only table: a (plan_name, feature_key) row means that plan grants the
feature by default. Absence = not granted (default-deny, same convention as
tenant.features). Resolution (see infrastructure/tenant/repository.py) is
additive: a tenant's effective feature set is its plan's defaults UNION its own
tenants.features overrides — a tenant can be granted extra features beyond its
plan, but tenants.features can't revoke a plan-granted feature. Revocable
per-tenant overrides are a bigger, separate feature (see PulseDesk's
tenant_feature_overrides) — out of scope here.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_features",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_name", sa.String(32), nullable=False),
        sa.Column("feature_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("plan_name", "feature_key", name="uq_plan_features_plan_key"),
    )
    op.create_index("ix_plan_features_plan_name", "plan_features", ["plan_name"])


def downgrade() -> None:
    op.drop_index("ix_plan_features_plan_name", "plan_features")
    op.drop_table("plan_features")
