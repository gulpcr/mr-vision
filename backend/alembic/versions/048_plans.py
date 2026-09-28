"""Subscription plans as first-class, platform-managed records.

Revision ID: 048
Revises: 047
Create Date: 2026-09-28

Until now a tenant's ``plan`` was a free-text string whose only effect was selecting
rows of ``plan_features`` (AI use-case keys) — and only while MULTI_TENANT_ENABLED was
on. ``plans`` makes a plan a record the platform admin defines:

* ``name`` (key, referenced by ``tenants.plan``) and ``display_name`` / ``description``;
* ``default_max_users`` — seat limit applied to tenants created on the plan;
* ``usecases`` — AI use cases the plan includes (NULL = every registered use case);
* ``permissions`` — the permission ceiling: the most any role in a tenant on this plan
  can do (``["*"]`` = no ceiling).

Every plan name already in use (tenants.plan, plan_features) is seeded with its
plan_features use cases (or all, if it had none) and no permission ceiling, so existing
tenants keep exactly their current access. ``plan_features`` is left in place, unused.
``plans`` is platform-level configuration: no tenant_id, no RLS.
"""
from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_max_users", sa.Integer(), nullable=True),
        sa.Column("usecases", sa.JSON(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default='["*"]'),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    conn = op.get_bind()
    names = {r[0] for r in conn.execute(sa.text("SELECT DISTINCT plan FROM tenants WHERE plan IS NOT NULL")).all()}
    names |= {r[0] for r in conn.execute(sa.text("SELECT DISTINCT plan_name FROM plan_features")).all()}
    names.add("starter")
    for name in sorted(names):
        keys = [r[0] for r in conn.execute(
            sa.text("SELECT feature_key FROM plan_features WHERE plan_name = :p ORDER BY feature_key"),
            {"p": name},
        ).all()]
        conn.execute(sa.text(
            "INSERT INTO plans (name, display_name, usecases, permissions) "
            "VALUES (:n, :d, CAST(:u AS json), '[\"*\"]')"
        ), {"n": name, "d": name.replace("_", " ").title(), "u": json.dumps(keys) if keys else None})

    op.create_foreign_key(
        "fk_tenants_plan", "tenants", "plans", ["plan"], ["name"],
        onupdate="CASCADE", ondelete="RESTRICT",
    )

    app_user = os.environ.get("POSTGRES_APP_USER", "").strip()
    if app_user and conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": app_user}).first():
        quoted = conn.execute(sa.text("SELECT quote_ident(:u)"), {"u": app_user}).scalar_one()
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON plans TO {quoted}"))


def downgrade() -> None:
    op.drop_constraint("fk_tenants_plan", "tenants", type_="foreignkey")
    op.drop_table("plans")
