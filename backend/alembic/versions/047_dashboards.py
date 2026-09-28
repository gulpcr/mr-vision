"""Per-tenant, per-role and per-user dashboards.

Revision ID: 047
Revises: 046
Create Date: 2026-09-25

``dashboard_layouts`` holds three kinds of dashboard, all tenant-owned (RLS):

* role defaults — ``owner_id`` NULL, ``is_default`` true, ``role_default`` = a role name;
  seeded for every tenant from app.domain.dashboards.ROLE_DEFAULT_DASHBOARDS and editable
  by that tenant's admins, so each hospital can tailor what its radiologists, doctors,
  technicians and receptionists see first;
* personal dashboards — ``owner_id`` set;
* shared dashboards — personal ones with ``is_shared`` (visible to the whole tenant).

``dashboard_versions`` snapshots the previous state on every save, for restore.
"""
from __future__ import annotations

import json
import os
import uuid

import sqlalchemy as sa
from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None

TENANT_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)


def upgrade() -> None:
    op.create_table(
        "dashboard_layouts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("role_default", sa.String(64), nullable=True),
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("widgets", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("filters", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("refresh_interval", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("updated_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dashboard_layouts_tenant_id", "dashboard_layouts", ["tenant_id"])
    op.create_index("ix_dashboard_layouts_owner_id", "dashboard_layouts", ["owner_id"])
    op.create_index(
        "uq_dashboard_role_default", "dashboard_layouts", ["tenant_id", "role_default"],
        unique=True, postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "dashboard_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dashboard_id", sa.String(36), sa.ForeignKey("dashboard_layouts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dashboard_id", "version_number", name="uq_dashboard_version"),
    )
    op.create_index("ix_dashboard_versions_tenant_id", "dashboard_versions", ["tenant_id"])

    conn = op.get_bind()
    for table in ("dashboard_layouts", "dashboard_versions"):
        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
        ))

    from app.domain.dashboards import ROLE_DEFAULT_DASHBOARDS

    for (tenant_id,) in conn.execute(sa.text("SELECT id FROM tenants")).all():
        for role, spec in ROLE_DEFAULT_DASHBOARDS.items():
            conn.execute(sa.text(
                "INSERT INTO dashboard_layouts (id, tenant_id, name, is_default, role_default, "
                "widgets, filters) VALUES (:id, :t, :n, true, :r, CAST(:w AS json), '{}') "
                "ON CONFLICT DO NOTHING"
            ), {"id": str(uuid.uuid4()), "t": tenant_id, "n": spec["name"], "r": role,
                "w": json.dumps(spec["widgets"])})

    app_user = os.environ.get("POSTGRES_APP_USER", "").strip()
    if app_user and conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": app_user}).first():
        quoted = conn.execute(sa.text("SELECT quote_ident(:u)"), {"u": app_user}).scalar_one()
        for table in ("dashboard_layouts", "dashboard_versions"):
            conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}"))


def downgrade() -> None:
    op.drop_table("dashboard_versions")
    op.drop_table("dashboard_layouts")
