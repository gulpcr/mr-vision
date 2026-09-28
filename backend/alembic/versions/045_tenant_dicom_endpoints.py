"""Per-tenant DICOM endpoints (AE titles) and a cross-tenant study-owner check.

Revision ID: 045
Revises: 044
Create Date: 2026-09-25

Orthanc is one PACS shared by every tenant. A modality pushing C-STORE carries no tenant,
so each tenant is given its own *called* AE title (``tenant_dicom_endpoints``): the site
configures its scanners to send to e.g. ``HOSPA_AI``, and the stable-study webhook
attributes the study to the tenant owning that AE title (read back from Orthanc's
per-instance ``CalledAET`` metadata). An optional ``calling_aet`` narrows the match to a
specific scanner.

``study_owner_tenant(uid)`` is SECURITY DEFINER: under Row-Level Security a tenant-scoped
session cannot see that another tenant already owns a StudyInstanceUID, yet uploads must
refuse such a UID *before* pushing instances into the shared PACS (which would otherwise
merge them into the other tenant's study). It returns only the owning tenant id (or the
tenant of a pending, not-yet-ingested upload), never study content.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None

POLICY_EXPR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR current_setting('app.platform', true) = 'on'"
)


def upgrade() -> None:
    op.create_table(
        "tenant_dicom_endpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("called_aet", sa.String(16), nullable=False),
        sa.Column("calling_aet", sa.String(16), nullable=True),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_tenant_dicom_endpoints_tenant_id", "tenant_dicom_endpoints", ["tenant_id"])
    # One tenant per (called, calling) pair; COALESCE so a NULL calling_aet (= any
    # scanner) is unique too.
    op.execute(
        "CREATE UNIQUE INDEX uq_tenant_dicom_endpoints_aet ON tenant_dicom_endpoints "
        "(upper(called_aet), upper(COALESCE(calling_aet, '')))"
    )

    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE tenant_dicom_endpoints ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text("ALTER TABLE tenant_dicom_endpoints FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text(
        f"CREATE POLICY tenant_isolation ON tenant_dicom_endpoints FOR ALL "
        f"USING ({POLICY_EXPR}) WITH CHECK ({POLICY_EXPR})"
    ))

    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION study_owner_tenant(uid varchar)
        RETURNS varchar
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT COALESCE(
                (SELECT s.tenant_id FROM studies s WHERE s.study_instance_uid = uid),
                (SELECT p.tenant_id FROM pending_study_tenants p WHERE p.study_instance_uid = uid)
            )
        $$
    """))
    conn.execute(sa.text("REVOKE ALL ON FUNCTION study_owner_tenant(varchar) FROM PUBLIC"))

    app_user = os.environ.get("POSTGRES_APP_USER", "").strip()
    if app_user:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": app_user}
        ).first()
        if exists:
            quoted = conn.execute(sa.text("SELECT quote_ident(:u)"), {"u": app_user}).scalar_one()
            conn.execute(sa.text(f"GRANT EXECUTE ON FUNCTION study_owner_tenant(varchar) TO {quoted}"))
            conn.execute(sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_dicom_endpoints TO {quoted}"
            ))


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS study_owner_tenant(varchar)")
    op.drop_table("tenant_dicom_endpoints")
