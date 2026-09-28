"""Postgres Row-Level Security for tenant isolation.

Revision ID: 044
Revises: 043
Create Date: 2026-09-25

Until now tenant isolation lived only in application code (repository ``_scope()``
filters) — any query that bypassed the repositories could read another tenant's rows,
and several did. This moves the boundary into the database:

1. ``tenant_id`` becomes NOT NULL with no server default on every tenant-owned table.
   The ``'default'`` server default silently filed any row whose writer forgot the
   tenant under the Admin tenant; a missing tenant is now a loud error instead. Rows are
   stamped from the bound DB scope by the ``before_flush`` hook in
   infrastructure/database/session.py.
2. ``batch_upload_items`` and ``alert_history`` get their own ``tenant_id`` (backfilled
   from their parent row) so they can carry a policy too.
3. ``audit_chain_tail()`` — a SECURITY DEFINER function returning the global hash-chain
   tail. The audit chain is one linear sequence across all tenants
   (PgAuditRepository.save); under RLS a tenant-scoped writer could otherwise only see its
   own tenant's last row and would fork the chain. The function exposes only
   ``(seq, row_hash)``, never row content.
4. RLS is ENABLEd and FORCEd on every tenant table with one policy: a row is visible and
   writable iff ``tenant_id = current_setting('app.tenant_id')`` or
   ``current_setting('app.platform') = 'on'``. Both GUCs are SET LOCAL per transaction
   from infrastructure/tenant/db_scope.py. An unset scope matches nothing (fail closed).
5. If ``POSTGRES_APP_USER`` / ``POSTGRES_APP_PASSWORD`` are set in the environment, the
   non-superuser, non-owner, NOBYPASSRLS application role is created (or its password
   updated) and granted DML. **RLS only binds that role**: the owner/superuser that runs
   migrations (``POSTGRES_USER``) bypasses policies by design, so until the app is
   pointed at ``POSTGRES_APP_USER`` (config.py) the policies are installed but inert and
   behaviour is unchanged.

No column or table is dropped by the upgrade.
"""
from __future__ import annotations

import os
import re

import sqlalchemy as sa
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

# Every table whose rows belong to exactly one tenant.
TENANT_TABLES = [
    "studies",
    "series",
    "job_runs",
    "results_index",
    "observations",
    "conditions",
    "audit_log",
    "users",
    "patients",
    "orders",
    "roles",
    "user_roles",
    "batch_uploads",
    "batch_upload_items",
    "review_queue",
    "alert_rules",
    "alert_history",
    "retention_policies",
    "share_links",
    "critical_alerts",
    "mammography_reports",
    "mri_reports",
    "tenant_api_keys",
    "pending_study_tenants",
]

# (child table, parent table, child FK column) — tenant_id added and backfilled here.
NEW_TENANT_COLUMNS = [
    ("batch_upload_items", "batch_uploads", "batch_id"),
    ("alert_history", "alert_rules", "rule_id"),
]

POLICY = "tenant_isolation"
POLICY_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)

_ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _app_role() -> tuple[str, str] | None:
    user = os.environ.get("POSTGRES_APP_USER", "").strip()
    password = os.environ.get("POSTGRES_APP_PASSWORD", "")
    if not user or not password:
        return None
    if not _ROLE_NAME_RE.match(user):
        raise ValueError(f"POSTGRES_APP_USER {user!r} is not a safe Postgres role name")
    return user, password


def upgrade() -> None:
    conn = op.get_bind()

    # 2. tenant_id on child tables, backfilled from the parent row.
    for child, parent, fk in NEW_TENANT_COLUMNS:
        op.add_column(child, sa.Column("tenant_id", sa.String(36), nullable=True))
        conn.execute(sa.text(
            f"UPDATE {child} c SET tenant_id = p.tenant_id "
            f"FROM {parent} p WHERE c.{fk} = p.id"
        ))
        op.create_index(f"ix_{child}_tenant_id", child, ["tenant_id"])

    # 1. NOT NULL, no default. Backfill any NULL to the Admin tenant ('default' is its
    #    id — see 043) first; that is where the old server default would have put them.
    for table in TENANT_TABLES:
        conn.execute(sa.text(f"UPDATE {table} SET tenant_id = 'default' WHERE tenant_id IS NULL"))
        op.alter_column(table, "tenant_id", existing_type=sa.String(36), nullable=False)
        conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN tenant_id DROP DEFAULT"))

    # 3. Global audit hash-chain tail, readable regardless of the caller's RLS scope.
    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION audit_chain_tail()
        RETURNS TABLE(seq integer, row_hash varchar)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT a.seq, a.row_hash
            FROM audit_log a
            WHERE a.seq IS NOT NULL
            ORDER BY a.seq DESC
            LIMIT 1
            FOR UPDATE
        $$
    """))
    conn.execute(sa.text("REVOKE ALL ON FUNCTION audit_chain_tail() FROM PUBLIC"))

    # 4. RLS.
    for table in TENANT_TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"DROP POLICY IF EXISTS {POLICY} ON {table}"))
        conn.execute(sa.text(
            f"CREATE POLICY {POLICY} ON {table} FOR ALL "
            f"USING ({POLICY_EXPR}) WITH CHECK ({POLICY_EXPR})"
        ))

    # 5. Application role (optional — see module docstring).
    role = _app_role()
    if role is not None:
        user, password = role
        # DDL can't take bind parameters, so the credentials travel through
        # transaction-local GUCs (properly bound) and are quoted by format(%I / %L)
        # inside the DO block — never interpolated into SQL text here.
        conn.execute(
            sa.text("SELECT set_config('mrv.app_user', :u, true), set_config('mrv.app_pw', :p, true)"),
            {"u": user, "p": password},
        )
        conn.execute(sa.text("""
            DO $$
            DECLARE
                u text := current_setting('mrv.app_user');
                p text := current_setting('mrv.app_pw');
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = u) THEN
                    EXECUTE format(
                        'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
                        u, p);
                ELSE
                    EXECUTE format(
                        'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
                        u, p);
                END IF;
            END
            $$
        """))
        grant_app_role(conn, user)


def grant_app_role(conn, user: str) -> None:
    """DML-only grants for the RLS-bound app role, including tables/sequences created by
    later migrations (ALTER DEFAULT PRIVILEGES applies to objects the migration owner
    creates from now on)."""
    conn.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {user}"))
    conn.execute(sa.text(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}"
    ))
    conn.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {user}"))
    conn.execute(sa.text(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {user}"
    ))
    conn.execute(sa.text(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {user}"
    ))
    conn.execute(sa.text(f"GRANT EXECUTE ON FUNCTION audit_chain_tail() TO {user}"))


def downgrade() -> None:
    conn = op.get_bind()
    for table in TENANT_TABLES:
        conn.execute(sa.text(f"DROP POLICY IF EXISTS {POLICY} ON {table}"))
        conn.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET DEFAULT 'default'"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS audit_chain_tail()"))
    for child, _parent, _fk in NEW_TENANT_COLUMNS:
        op.drop_index(f"ix_{child}_tenant_id", child)
        op.drop_column(child, "tenant_id")
