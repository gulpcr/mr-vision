"""Break-glass emergency access (HIPAA 164.312(a)(2)(ii) emergency access procedure).

Revision ID: 053
Revises: 052
Create Date: 2026-09-29

A referral-scoped user (the referring Doctor) normally sees only patients they referred.
In an emergency they may open one patient's studies outside that scope for a limited
time, stating a reason. Each grant is a ``break_glass_grants`` row; it is audited and
reviewed by the workspace's administrators, and can be revoked early.

* ``break_glass_grants`` (tenant-isolated; a referral-scoped user sees only their own).
  No DELETE for the application role: grants are evidence.
* ``studies.referral_scope`` now also admits studies of a patient (MRN) the user holds an
  active grant for. Study-derived tables follow automatically (their policies select
  through ``studies``).
* Permissions ``break_glass.invoke`` (granted to the doctor system role in every tenant)
  and ``break_glass.review`` (admins hold it through the wildcard).
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None

TENANT_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)
NO_REFERRAL_SCOPE = "coalesce(current_setting('app.referring_user', true), '') = ''"
ACTIVE_GRANT_PATIENTS = (
    "SELECT g.patient_id FROM break_glass_grants g "
    "WHERE g.user_id = current_setting('app.referring_user', true) "
    "AND g.revoked_at IS NULL AND g.expires_at > now()"
)


def _app_role(conn) -> str | None:
    user = os.environ.get("POSTGRES_APP_USER", "").strip()
    if not user:
        return None
    if conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": user}).first():
        return conn.execute(sa.text("SELECT quote_ident(:u)"), {"u": user}).scalar_one()
    return None


def upgrade() -> None:
    conn = op.get_bind()
    op.create_table(
        "break_glass_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("patient_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(128), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
    )
    op.create_index("ix_break_glass_active", "break_glass_grants", ["user_id", "patient_id", "expires_at"])
    op.create_index("ix_break_glass_tenant_created", "break_glass_grants", ["tenant_id", "created_at"])

    conn.execute(sa.text("ALTER TABLE break_glass_grants ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text("ALTER TABLE break_glass_grants FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text(
        f"CREATE POLICY tenant_isolation ON break_glass_grants FOR ALL "
        f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
    ))
    conn.execute(sa.text(
        f"CREATE POLICY referral_scope ON break_glass_grants AS RESTRICTIVE FOR SELECT USING ("
        f"{NO_REFERRAL_SCOPE} OR user_id = current_setting('app.referring_user', true))"
    ))

    conn.execute(sa.text("DROP POLICY IF EXISTS referral_scope ON studies"))
    conn.execute(sa.text(
        f"CREATE POLICY referral_scope ON studies AS RESTRICTIVE FOR SELECT USING ("
        f"{NO_REFERRAL_SCOPE} OR referring_user_id = current_setting('app.referring_user', true) "
        f"OR patient_id IN ({ACTIVE_GRANT_PATIENTS}))"
    ))

    conn.execute(sa.text(
        "UPDATE roles SET permissions = (permissions::jsonb || to_jsonb(CAST(:p AS text)))::json, "
        "updated_at = now() WHERE is_system AND name = 'doctor' AND NOT (permissions::jsonb ? :p)"
    ), {"p": "break_glass.invoke"})

    role = _app_role(conn)
    if role:
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON break_glass_grants TO {role}"))
        conn.execute(sa.text(f"REVOKE DELETE, TRUNCATE ON break_glass_grants FROM {role}"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP POLICY IF EXISTS referral_scope ON studies"))
    conn.execute(sa.text(
        f"CREATE POLICY referral_scope ON studies AS RESTRICTIVE FOR SELECT USING ("
        f"{NO_REFERRAL_SCOPE} OR referring_user_id = current_setting('app.referring_user', true))"
    ))
    op.drop_index("ix_break_glass_tenant_created", table_name="break_glass_grants")
    op.drop_index("ix_break_glass_active", table_name="break_glass_grants")
    op.drop_table("break_glass_grants")
