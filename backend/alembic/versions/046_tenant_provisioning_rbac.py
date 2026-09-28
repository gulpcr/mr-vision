"""Tenant provisioning, per-tenant accounts, settings/branding, referral-scoped RBAC.

Revision ID: 046
Revises: 045
Create Date: 2026-09-25

1. **Per-tenant accounts.** ``users.username`` / ``users.email`` were unique across the
   whole platform, so two hospitals could not both have a "jsmith". They become unique
   per tenant (login resolves the workspace first). Adds invitation columns (hashed
   one-time token + expiry + ``status``) and ``token_version`` (bumped to revoke every
   issued token for a user).
2. **Tenant lifecycle.** ``tenants.max_users`` (seat limit enforced at invite) and
   ``deleted_at``.
3. **tenant_settings / tenant_branding** — one row per tenant: report header/footer and
   signatories used by PDF reports, timezone; display name, logo and colours for the UI.
4. **Referral scoping for the referring Doctor.** ``orders.referring_user_id`` (chosen at
   intake) and ``studies.referring_user_id`` (copied when an order is linked to a study).
   Additional RESTRICTIVE policies: when the ``app.referring_user`` GUC is set (only for
   users holding ``study.view.referred`` without ``study.view``), studies are visible
   only if referred by that user, every study-derived table only for visible studies, and
   patient-level clinical tables not at all.
5. **System roles in every tenant** (tenant creation never seeded any, leaving non-admin
   users of new tenants with no permissions): inserts any missing system role and
   re-syncs system-role permissions to the catalog (admin → ``*``; new ``doctor`` role).

No column or table is dropped by the upgrade (only the two global UNIQUE constraints).
"""
from __future__ import annotations

import json
import os
import uuid

import sqlalchemy as sa
from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None

TENANT_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)
NO_REFERRAL_SCOPE = "coalesce(current_setting('app.referring_user', true), '') = ''"

# Tables whose rows derive from a study — visible to a referral-scoped user only for
# studies they can see.
STUDY_DERIVED = [
    "series", "job_runs", "results_index", "mammography_reports", "mri_reports",
    "critical_alerts", "review_queue", "share_links", "observations", "conditions",
]
# Patient-level tables a referral-scoped user never sees directly.
REFERRAL_HIDDEN = ["patients"]


def _app_role(conn) -> str | None:
    user = os.environ.get("POSTGRES_APP_USER", "").strip()
    if not user:
        return None
    if conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": user}).first():
        return conn.execute(sa.text("SELECT quote_ident(:u)"), {"u": user}).scalar_one()
    return None


def _tenant_table_rls(conn, table: str) -> None:
    conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
        f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
    ))


def upgrade() -> None:
    conn = op.get_bind()

    # 1. users
    conn.execute(sa.text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_username_key"))
    conn.execute(sa.text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key"))
    op.create_index("uq_users_tenant_username", "users", ["tenant_id", "username"], unique=True)
    op.create_index("uq_users_tenant_email", "users", ["tenant_id", "email"], unique=True)
    op.add_column("users", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("users", sa.Column("invitation_token_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("invitation_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_users_invitation_token_hash", "users", ["invitation_token_hash"])

    # 2. tenants
    op.add_column("tenants", sa.Column("max_users", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    # 3. settings / branding
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("institution_name", sa.String(256), nullable=True),
        sa.Column("institution_address", sa.Text(), nullable=True),
        sa.Column("report_header", sa.Text(), nullable=True),
        sa.Column("report_footer", sa.Text(), nullable=True),
        sa.Column("signatory_name", sa.String(256), nullable=True),
        sa.Column("signatory_title", sa.String(256), nullable=True),
        sa.Column("signatory_qualifications", sa.String(256), nullable=True),
        sa.Column("secondary_signatory_name", sa.String(256), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "tenant_branding",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("logo_data_url", sa.Text(), nullable=True),
        sa.Column("primary_color", sa.String(7), nullable=True),
        sa.Column("accent_color", sa.String(7), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("primary_color IS NULL OR primary_color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_branding_primary_hex"),
        sa.CheckConstraint("accent_color IS NULL OR accent_color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_branding_accent_hex"),
    )
    for table in ("tenant_settings", "tenant_branding"):
        _tenant_table_rls(conn, table)
    conn.execute(sa.text(
        "INSERT INTO tenant_settings (tenant_id) SELECT id FROM tenants ON CONFLICT DO NOTHING"
    ))
    conn.execute(sa.text(
        "INSERT INTO tenant_branding (tenant_id, display_name) SELECT id, name FROM tenants "
        "ON CONFLICT DO NOTHING"
    ))

    # 4. referral link
    op.add_column("orders", sa.Column(
        "referring_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    ))
    op.add_column("studies", sa.Column(
        "referring_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    ))
    op.create_index("ix_orders_referring_user_id", "orders", ["referring_user_id"])
    op.create_index("ix_studies_referring_user_id", "studies", ["referring_user_id"])

    # 5. referral-scope restrictive policies (AND-ed with tenant_isolation)
    conn.execute(sa.text(
        f"CREATE POLICY referral_scope ON studies AS RESTRICTIVE FOR SELECT USING ("
        f"{NO_REFERRAL_SCOPE} OR referring_user_id = current_setting('app.referring_user', true))"
    ))
    conn.execute(sa.text(
        f"CREATE POLICY referral_scope ON orders AS RESTRICTIVE FOR SELECT USING ("
        f"{NO_REFERRAL_SCOPE} OR referring_user_id = current_setting('app.referring_user', true))"
    ))
    for table in STUDY_DERIVED:
        # The sub-select on studies is itself filtered by studies' policies, so this
        # yields exactly the studies the referral-scoped user may see.
        conn.execute(sa.text(
            f"CREATE POLICY referral_scope ON {table} AS RESTRICTIVE FOR SELECT USING ("
            f"{NO_REFERRAL_SCOPE} OR study_instance_uid IN (SELECT s.study_instance_uid FROM studies s))"
        ))
    for table in REFERRAL_HIDDEN:
        conn.execute(sa.text(
            f"CREATE POLICY referral_scope ON {table} AS RESTRICTIVE FOR SELECT USING ({NO_REFERRAL_SCOPE})"
        ))

    # 6. system roles in every tenant
    from app.domain.permissions import SYSTEM_ROLE_PERMISSIONS

    tenant_ids = [r[0] for r in conn.execute(sa.text("SELECT id FROM tenants")).all()]
    for tenant_id in tenant_ids:
        for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
            conn.execute(sa.text(
                "INSERT INTO roles (id, tenant_id, name, permissions, is_system, created_at, updated_at) "
                "VALUES (:id, :t, :n, CAST(:p AS json), true, now(), now()) "
                "ON CONFLICT (tenant_id, name) DO UPDATE SET permissions = EXCLUDED.permissions, "
                "is_system = true, updated_at = now()"
            ), {"id": str(uuid.uuid4()), "t": tenant_id, "n": name, "p": json.dumps(perms)})

    role = _app_role(conn)
    if role:
        for table in ("tenant_settings", "tenant_branding"):
            conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"))


def downgrade() -> None:
    conn = op.get_bind()
    for table in ["studies", "orders", *STUDY_DERIVED, *REFERRAL_HIDDEN]:
        conn.execute(sa.text(f"DROP POLICY IF EXISTS referral_scope ON {table}"))
    op.drop_index("ix_studies_referring_user_id", "studies")
    op.drop_index("ix_orders_referring_user_id", "orders")
    op.drop_column("studies", "referring_user_id")
    op.drop_column("orders", "referring_user_id")
    op.drop_table("tenant_branding")
    op.drop_table("tenant_settings")
    op.drop_column("tenants", "deleted_at")
    op.drop_column("tenants", "max_users")
    op.drop_index("ix_users_invitation_token_hash", "users")
    for col in ("token_version", "invitation_expires_at", "invitation_token_hash", "status"):
        op.drop_column("users", col)
    op.drop_index("uq_users_tenant_email", "users")
    op.drop_index("uq_users_tenant_username", "users")
