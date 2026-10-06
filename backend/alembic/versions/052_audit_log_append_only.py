"""Append-only audit log with a six-year retention floor (HIPAA 164.312(b), 164.316(b)(2)).

Revision ID: 052
Revises: 051
Create Date: 2026-09-29

* The application role loses UPDATE / DELETE / TRUNCATE on ``audit_log`` (it keeps
  SELECT + INSERT), so no application path — or anyone holding its credentials — can
  rewrite or remove history.
* A trigger rejects every UPDATE and TRUNCATE for all roles, including the owner, and
  every DELETE except a deliberate retention purge of rows older than six years:
  ``SET LOCAL app.audit_retention_purge = 'on'`` inside the purging transaction. Row-level
  tampering is additionally detectable through the HMAC hash chain.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def _app_role(conn) -> str | None:
    user = os.environ.get("POSTGRES_APP_USER", "").strip()
    if not user:
        return None
    if conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": user}).first():
        return conn.execute(sa.text("SELECT quote_ident(:u)"), {"u": user}).scalar_one()
    return None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION audit_log_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF coalesce(current_setting('app.audit_retention_purge', true), '') = 'on'
                   AND OLD."timestamp" < now() - interval '6 years' THEN
                    RETURN OLD;
                END IF;
            END IF;
            RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
    """))
    conn.execute(sa.text("""
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_guard()
    """))
    conn.execute(sa.text("""
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_guard()
    """))
    role = _app_role(conn)
    if role:
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {role}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON audit_log TO {role}"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log"))
    conn.execute(sa.text("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS audit_log_guard()"))
