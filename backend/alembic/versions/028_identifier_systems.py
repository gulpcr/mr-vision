"""Referential link study→patient, and an external order identifier.

Revision ID: 028
Revises: 027
Create Date: 2026-07-29

FHIR_R4_COMPLIANCE_AUDIT.md step 2. Two additive columns:

``studies.patient_record_id`` → ``patients.id``
    A study's patient was only ever discoverable by matching ``studies.patient_id``
    (the raw DICOM PatientID) against ``patients.patient_ref`` as strings — see the
    fallback in OnboardingService.get_clinical_for_study. That join has no referential
    integrity, so a modality that strips a leading zero or adds a site prefix silently
    orphans the study, and nothing reports it. Every exported resource's ``subject``
    depends on this link, which makes it the highest-consequence missing constraint in
    the schema. ``patient_id`` stays exactly as it is — it remains the *identifier*
    (the value the modality sent); this new column is the *reference*.

``orders.external_order_ref``
    The placer's own order number (HL7 ORC-2 / FHIR ServiceRequest.identifier) for
    orders that arrive from a RIS/EHR instead of being typed at reception. Nullable:
    reception-entered orders have no external number.

Backfill
--------
``patient_record_id`` is backfilled only where the MRN match is **unambiguous and
tenant-scoped**: exactly one patient row in the same tenant whose ``patient_ref``
equals the study's ``patient_id``. Ambiguous and zero-match studies are deliberately
left NULL — a wrong patient link is far worse than a missing one, so this migration
never guesses. (Note the pre-existing auto-link in OnboardingService.create_order
matches MRNs *without* a tenant filter; the backfill here does not copy that bug.)

Additive only: no column or table is dropped, no existing value is modified.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


# Exactly-one-candidate, same-tenant MRN match. A study with two same-MRN patient rows
# across tenants, or none, is left NULL for a human to resolve.
_BACKFILL_SQL = """
UPDATE studies s
   SET patient_record_id = p.id
  FROM patients p
 WHERE p.patient_ref = s.patient_id
   AND p.tenant_id = COALESCE(s.tenant_id, 'default')
   AND s.patient_id IS NOT NULL
   AND s.patient_record_id IS NULL
   AND (
         SELECT COUNT(*) FROM patients p2
          WHERE p2.patient_ref = s.patient_id
            AND p2.tenant_id = COALESCE(s.tenant_id, 'default')
       ) = 1
"""


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "studies" in tables and "patient_record_id" not in _columns(bind, "studies"):
        op.add_column("studies", sa.Column("patient_record_id", sa.String(36), nullable=True))
        op.create_index("ix_studies_patient_record_id", "studies", ["patient_record_id"])
        if "patients" in tables:
            op.create_foreign_key(
                "fk_studies_patient_record_id",
                "studies",
                "patients",
                ["patient_record_id"],
                ["id"],
                ondelete="SET NULL",
            )
            result = bind.execute(sa.text(_BACKFILL_SQL))
            print(f"028: linked {result.rowcount} study rows to a patient by unambiguous MRN")

    if "orders" in tables and "external_order_ref" not in _columns(bind, "orders"):
        op.add_column("orders", sa.Column("external_order_ref", sa.String(128), nullable=True))
        op.create_index("ix_orders_external_order_ref", "orders", ["external_order_ref"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "orders" in tables and "external_order_ref" in _columns(bind, "orders"):
        op.drop_index("ix_orders_external_order_ref", table_name="orders")
        op.drop_column("orders", "external_order_ref")

    if "studies" in tables and "patient_record_id" in _columns(bind, "studies"):
        if "patients" in tables:
            op.drop_constraint("fk_studies_patient_record_id", "studies", type_="foreignkey")
        op.drop_index("ix_studies_patient_record_id", table_name="studies")
        op.drop_column("studies", "patient_record_id")
