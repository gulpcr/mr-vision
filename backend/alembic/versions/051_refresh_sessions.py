"""Refresh sessions (HIPAA 164.312(a)(2)(iii) automatic logoff, 164.312(d)).

Revision ID: 051
Revises: 050
Create Date: 2026-09-29

Access tokens now live 15 minutes; a session continues only through a rotating refresh
token held in an httpOnly cookie. One row per issued refresh token:

* ``token_hash`` — SHA-256 of the opaque token (the token itself is never stored).
* ``family_id`` — all tokens rotated from one sign-in; presenting an already-rotated
  (or revoked) token revokes the whole family (refresh-token reuse detection).
* ``token_version`` — the user's token_version at issue; a password change / admin
  revoke bumps it and so ends the session at its next refresh.
* ``last_used_at`` / ``expires_at`` — server-enforced idle timeout and absolute lifetime.

Rows are revoked (``revoked_at``) rather than deleted, so the app role gets no DELETE.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None

TENANT_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
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
        "refresh_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(32), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
    )
    op.create_index("ix_refresh_sessions_user", "refresh_sessions", ["user_id", "revoked_at"])
    op.create_index("ix_refresh_sessions_family", "refresh_sessions", ["family_id"])

    conn.execute(sa.text("ALTER TABLE refresh_sessions ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text("ALTER TABLE refresh_sessions FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text(
        f"CREATE POLICY tenant_isolation ON refresh_sessions FOR ALL "
        f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
    ))
    role = _app_role(conn)
    if role:
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON refresh_sessions TO {role}"))
        conn.execute(sa.text(f"REVOKE DELETE, TRUNCATE ON refresh_sessions FROM {role}"))


def downgrade() -> None:
    op.drop_index("ix_refresh_sessions_family", table_name="refresh_sessions")
    op.drop_index("ix_refresh_sessions_user", table_name="refresh_sessions")
    op.drop_table("refresh_sessions")
