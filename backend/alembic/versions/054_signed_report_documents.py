"""Frozen, hash-verified PDF of every e-signed report.

Revision ID: 054
Revises: 053
Create Date: 2026-10-01

``report_signatures`` (049) hashes the *content* that was signed. A re-rendered PDF is
not byte-reproducible (render timestamps, fonts), so the document itself is frozen: the
first PDF produced for a signed study (while its content still matches the signature)
is stored in the artifact store and recorded here with its SHA-256. Every later download
of that report serves the stored copy after re-verifying the hash (HIPAA 164.312(c)(1)
integrity, (c)(2) mechanism to authenticate ePHI).

Append-only for the application role, like report_signatures; tenant isolation and the
referral scope policy as in 049.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None

TABLE = "report_signature_documents"
TENANT_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)
NO_REFERRAL_SCOPE = "coalesce(current_setting('app.referring_user', true), '') = ''"


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
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "signature_id", sa.String(36),
            sa.ForeignKey("report_signatures.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "study_instance_uid", sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("usecase", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("signature_id", "usecase", name="uq_signature_document_usecase"),
    )
    op.create_index(f"ix_{TABLE}_study", TABLE, ["study_instance_uid"])
    conn.execute(sa.text(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text(
        f"CREATE POLICY tenant_isolation ON {TABLE} FOR ALL "
        f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
    ))
    conn.execute(sa.text(
        f"CREATE POLICY referral_scope ON {TABLE} AS RESTRICTIVE FOR SELECT USING ("
        f"{NO_REFERRAL_SCOPE} OR study_instance_uid IN (SELECT s.study_instance_uid FROM studies s))"
    ))
    role = _app_role(conn)
    if role:
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON {TABLE} TO {role}"))
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON {TABLE} FROM {role}"))


def downgrade() -> None:
    op.drop_table(TABLE)
