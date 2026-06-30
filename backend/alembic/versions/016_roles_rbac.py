"""RBAC roles table + system-role seed + admin user seed.

Revision ID: 016
Revises: 015
Create Date: 2026-06-22

Adds the per-tenant `roles` table (permission sets), seeds the five system roles
(admin, receptionist, technician, radiologist, viewer) for the default tenant,
and seeds an `admin` / `admin123` administrator if no admin user exists yet.
"""
from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def _hash_password(password: str) -> str:
    # pbkdf2_sha256 — matches AuthService (bcrypt is broken with passlib in this
    # image: passlib 1.7.4 vs bcrypt >= 4.1). Pure-Python, no native dependency.
    from passlib.context import CryptContext

    return CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto").hash(password)


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])
    op.create_index("ix_roles_tenant_name", "roles", ["tenant_id", "name"], unique=True)

    # Seed system roles for the default tenant (source of truth: domain.permissions).
    from app.domain.permissions import SYSTEM_ROLE_PERMISSIONS

    bind = op.get_bind()
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        bind.execute(
            sa.text(
                "INSERT INTO roles (id, tenant_id, name, permissions, is_system) "
                "VALUES (:id, 'default', :name, CAST(:perms AS JSON), true)"
            ),
            {"id": str(uuid.uuid4()), "name": name, "perms": json.dumps(perms)},
        )

    # Seed the admin user only if no admin exists (idempotent / non-destructive).
    existing_admin = bind.execute(
        sa.text("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")
    ).first()
    if not existing_admin:
        bind.execute(
            sa.text(
                "INSERT INTO users "
                "(id, username, email, hashed_password, full_name, role, tenant_id, is_active) "
                "VALUES (:id, 'admin', 'admin@local', :pw, 'Administrator', 'admin', 'default', true)"
            ),
            {"id": str(uuid.uuid4()), "pw": _hash_password("admin123")},
        )


def downgrade() -> None:
    op.execute("DELETE FROM users WHERE username = 'admin'")
    op.drop_index("ix_roles_tenant_name", table_name="roles")
    op.drop_index("ix_roles_tenant_id", table_name="roles")
    op.drop_table("roles")
