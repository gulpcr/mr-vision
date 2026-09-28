"""Electronic report signatures, report comments and review-queue priority override.

Revision ID: 049
Revises: 048
Create Date: 2026-09-28

* ``report_signatures`` — one row per electronic signature. Stores who signed (id,
  username, full name and role at signing time), the attestation statement version *and*
  its exact rendered text, the signer's mandatory comment, and a SHA-256 over a snapshot
  of the report content that was signed (latest AI results + the editable mammography
  report). The snapshot is kept so a later re-run can be detected ("changed since
  signing") and the signed content reproduced. Rows are never updated or deleted by the
  application.
* ``report_comments`` — free-text comments on a study's report (radiologists, referring
  doctors).
* ``studies.priority_override`` (+ by / at) — a radiologist's manual override of the
  computed review-queue priority (critical | abnormal | normal).
* New permission ``report.comment``, granted to the radiologist and doctor system roles
  in every tenant.

Both new tables carry the tenant_isolation policy and the study-derived referral_scope
policy (alembic 044 / 046), so a referring doctor sees signatures and comments only on
studies they referred.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None

TENANT_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)
NO_REFERRAL_SCOPE = "coalesce(current_setting('app.referring_user', true), '') = ''"
TABLES = ("report_signatures", "report_comments")
NEW_PERMISSION = "report.comment"
GRANTED_TO = ("radiologist", "doctor")


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
        "report_signatures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "study_instance_uid", sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("signer_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("signer_username", sa.String(128), nullable=False),
        sa.Column("signer_full_name", sa.String(256), nullable=False),
        sa.Column("signer_role", sa.String(64), nullable=True),
        sa.Column("statement_version", sa.String(32), nullable=False),
        sa.Column("statement_text", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_snapshot", sa.JSON(), nullable=False),
        sa.Column("priority_at_signing", sa.String(16), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_report_signatures_study", "report_signatures", ["study_instance_uid", "signed_at"])

    op.create_table(
        "report_comments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "study_instance_uid", sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("author_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_username", sa.String(128), nullable=False),
        sa.Column("author_full_name", sa.String(256), nullable=True),
        sa.Column("author_role", sa.String(64), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_report_comments_study", "report_comments", ["study_instance_uid", "created_at"])

    op.add_column("studies", sa.Column("priority_override", sa.String(16), nullable=True))
    op.add_column("studies", sa.Column("priority_override_by", sa.String(128), nullable=True))
    op.add_column("studies", sa.Column("priority_override_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_studies_priority_override", "studies",
        "priority_override IS NULL OR priority_override IN ('critical', 'abnormal', 'normal')",
    )

    for table in TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
        ))
        conn.execute(sa.text(
            f"CREATE POLICY referral_scope ON {table} AS RESTRICTIVE FOR SELECT USING ("
            f"{NO_REFERRAL_SCOPE} OR study_instance_uid IN (SELECT s.study_instance_uid FROM studies s))"
        ))

    # report.comment for the radiologist and doctor system roles (json array append,
    # idempotent). Custom roles are left to the tenant admin.
    conn.execute(sa.text(
        "UPDATE roles SET permissions = (permissions::jsonb || to_jsonb(CAST(:p AS text)))::json, "
        "updated_at = now() "
        "WHERE is_system AND name = ANY(:names) AND NOT (permissions::jsonb ? :p)"
    ), {"p": NEW_PERMISSION, "names": list(GRANTED_TO)})

    role = _app_role(conn)
    if role:
        for table in TABLES:
            # Signatures are append-only for the application: no UPDATE / DELETE grant.
            grants = "SELECT, INSERT" if table == "report_signatures" else "SELECT, INSERT, UPDATE, DELETE"
            conn.execute(sa.text(f"GRANT {grants} ON {table} TO {role}"))
        # 044's ALTER DEFAULT PRIVILEGES already handed the new table full DML to the app
        # role; take UPDATE / DELETE back. (FK cascades from studies / users still work:
        # referential actions run with the table owner's privileges.)
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON report_signatures FROM {role}"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE roles SET permissions = (permissions::jsonb - CAST(:p AS text))::json "
        "WHERE is_system AND name = ANY(:names)"
    ), {"p": NEW_PERMISSION, "names": list(GRANTED_TO)})
    op.drop_constraint("ck_studies_priority_override", "studies", type_="check")
    op.drop_column("studies", "priority_override_at")
    op.drop_column("studies", "priority_override_by")
    op.drop_column("studies", "priority_override")
    op.drop_table("report_comments")
    op.drop_table("report_signatures")
