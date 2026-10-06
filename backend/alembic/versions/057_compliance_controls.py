"""Compliance controls batch: NPI on signatures, AI provenance, QA edit distance,
patient requests and restrictions, tenant BAA records.

Revision ID: 057
Revises: 056
Create Date: 2026-10-02

* ``report_signatures.signer_npi`` — the signer's validated NPI at signing time (TEC-08).
* ``ai_inference_metadata`` — one provenance row per AI result: model, weights SHA-256,
  frameworks, LLM digest, prompt hash, GPU, windowing, timestamps (AI-02). Append-only.
* ``ai_report_reviews`` — per signed AI result: word-level edit distance / similarity
  between AI draft and signed report, structured-slot discrepancies, outcome (AI-04).
  Numbers only, no report text.
* ``patient_requests`` — register of individual-rights requests (access, amendment,
  accounting, restriction, confidential communications): received, due (+30 days, one
  30-day extension), fulfilled/denied (PRV-04, 164.524(b)(2)).
* ``patient_restrictions`` — agreed 164.522 restrictions on disclosures, enforced on
  FHIR / DICOM export / webhooks / share links (PRV-04).
* ``tenant_baas`` — the BAA on file for each customer tenant (date, signatory,
  document SHA-256); with REQUIRE_TENANT_BAA a tenant is not activated and gets no DICOM
  endpoint or API key without an active one (ADM-01).
No DELETE for the app role on these three: history is kept, rows are closed instead.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
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


def _tenant_rls(conn, table: str) -> None:
    conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
        f"USING ({TENANT_EXPR}) WITH CHECK ({TENANT_EXPR})"
    ))


def upgrade() -> None:
    conn = op.get_bind()
    op.add_column("report_signatures", sa.Column("signer_npi", sa.String(10), nullable=True))
    op.create_table(
        "ai_inference_metadata",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("result_id", sa.String(36), sa.ForeignKey("results_index.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("usecase_name", sa.String(128), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("record", sa.JSON(), nullable=False),
        sa.Column("record_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    _tenant_rls(conn, "ai_inference_metadata")
    role = _app_role(conn)
    if role:
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON ai_inference_metadata TO {role}"))
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON ai_inference_metadata FROM {role}"))
    op.create_table(
        "ai_report_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signature_id", sa.String(36), sa.ForeignKey("report_signatures.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("usecase_name", sa.String(128), nullable=False),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("draft_tokens", sa.Integer(), nullable=False),
        sa.Column("final_tokens", sa.Integer(), nullable=False),
        sa.Column("edit_distance", sa.Integer(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("slots_compared", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slots_mismatched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_report_reviews_tenant_time", "ai_report_reviews", ["tenant_id", "created_at"])
    _tenant_rls(conn, "ai_report_reviews")
    if role:
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON ai_report_reviews TO {role}"))
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON ai_report_reviews FROM {role}"))

    op.create_table(
        "patient_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mrn", sa.String(64), nullable=False),
        sa.Column("request_type", sa.String(32), nullable=False),
        sa.Column("requester", sa.String(256), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extension_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(128), nullable=True),
        sa.Column("outcome_notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'extended', 'fulfilled', 'denied')",
                           name="ck_patient_requests_status"),
    )
    op.create_index("ix_patient_requests_tenant_status", "patient_requests",
                    ["tenant_id", "status", "due_at"])
    _tenant_rls(conn, "patient_requests")

    op.create_table(
        "patient_restrictions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mrn", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("patient_requests.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("agreed_by", sa.String(128), nullable=False),
        sa.Column("agreed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(128), nullable=True),
    )
    op.create_index("ix_patient_restrictions_tenant_mrn", "patient_restrictions", ["tenant_id", "mrn"])
    _tenant_rls(conn, "patient_restrictions")

    op.create_table(
        "tenant_baas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("counterparty", sa.String(256), nullable=False),
        sa.Column("signatory_name", sa.String(256), nullable=False),
        sa.Column("signatory_title", sa.String(256), nullable=True),
        sa.Column("signed_on", sa.Date(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("document_name", sa.String(256), nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminated_by", sa.String(128), nullable=True),
    )
    op.create_index("ix_tenant_baas_tenant", "tenant_baas", ["tenant_id"])
    _tenant_rls(conn, "tenant_baas")
    if role:
        for table in ("patient_requests", "patient_restrictions", "tenant_baas"):
            conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}"))
            conn.execute(sa.text(f"REVOKE DELETE, TRUNCATE ON {table} FROM {role}"))


def downgrade() -> None:
    op.drop_table("tenant_baas")
    op.drop_table("patient_restrictions")
    op.drop_table("patient_requests")
    op.drop_table("ai_report_reviews")
    op.drop_table("ai_inference_metadata")
    op.drop_column("report_signatures", "signer_npi")
