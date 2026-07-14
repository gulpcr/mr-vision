"""Platform-operator flag — enables impersonation without full platform-admin power.

Revision ID: 033
Revises: 032
Create Date: 2026-07-13

Adds a distinct is_platform_operator flag alongside the existing
is_platform_admin (migration 030). Operators can impersonate any tenant's
user (time-limited, audited via ImpersonationService) for support purposes,
but cannot manage tenant lifecycle, promote/demote platform admins, or wipe
all-tenant data — those remain is_platform_admin-only. A platform admin is
implicitly also an operator (checked as is_platform_admin OR
is_platform_operator in require_platform_operator).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_platform_operator", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("users", "is_platform_operator")
