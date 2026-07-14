"""Platform-admin flag — closes cross-tenant admin access.

Revision ID: 030
Revises: 029
Create Date: 2026-07-08

Minimal platform/tenant admin separation: today any tenant's role="admin" user
can call /api/admin/tenants* for OTHER tenants too, since RBACMiddleware's
admin-path check is just role=="admin" with no tenant boundary. This adds a
distinct is_platform_admin flag (carried as a JWT claim, not tied to any one
tenant) required — on top of the existing role check — for tenant-management,
tenant-API-key, and plan-feature endpoints.

Deliberately NOT included (larger, separate initiative if needed later):
a distinct signing secret for platform tokens, operator roles (support/billing/
superadmin), MFA, an append-only audit spine, or impersonation.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("users", "is_platform_admin")
