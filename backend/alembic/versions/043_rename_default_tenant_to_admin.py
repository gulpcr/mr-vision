"""Rename the seeded 'default' tenant to 'Admin' / 'admin', bootstrap platform-admin.

Revision ID: 043
Revises: 042
Create Date: 2026-08-19

Every tenant_id column across the schema defaults to the literal string
"default", which is also the primary key of the tenant row seeded by
004_rbac.py. Rather than reassigning that id (which would require rewriting the
FK/plain tenant_id column on every table), this just renames the row's
display name/slug — every existing study/job/result/user already correctly
points at it via tenant_id='default'.

is_platform_admin defaults to false for every existing row (migration 038), so
without this, nobody could reach any require_platform_admin-gated endpoint
(tenant CRUD, audit verify, /reset?all_tenants=true) after this ships. Grants it
to the existing seeded 'admin' user only — is_platform_operator is deliberately
left false for everyone; grant it manually via the new admin panel afterward.
"""
from __future__ import annotations

from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE tenants SET name = 'Admin', slug = 'admin' WHERE id = 'default'")
    op.execute("UPDATE users SET is_platform_admin = true WHERE username = 'admin'")


def downgrade() -> None:
    op.execute("UPDATE users SET is_platform_admin = false WHERE username = 'admin'")
    op.execute("UPDATE tenants SET name = 'Default', slug = 'default' WHERE id = 'default'")
