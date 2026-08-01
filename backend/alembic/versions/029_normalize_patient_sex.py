"""Normalise studies.patient_sex onto the FHIR administrativeGender value set.

Revision ID: 029
Revises: 028
Create Date: 2026-07-29

FHIR_R4_COMPLIANCE_AUDIT.md step 3. ``studies.patient_sex`` held raw DICOM PatientSex
("M"/"F"/"O") while ``patients.sex`` held "male"/"female"/"other" — two representations
of one concept, in one database, with no reconciliation. Raw DICOM codes are not valid
``Patient.gender`` values, and code that needed a consistent answer re-derived the
mapping inline (infrastructure/queue/tasks.py did exactly that for LLM prompt context).

New rows are normalised at ingest (application/study_service.py); this backfills the
existing ones so both tables finally agree.

    M → male     F → female     O → other

Values that are neither those nor already-normalised are left untouched rather than
guessed at: some scanners emit "U", "1", or free text, and a wrong gender on a patient
record is worse than an unmapped one. Anything left behind is reported by the count
below and still renders correctly — both display formatters
(reports/pdf_generator._fmt_sex, ui/src/lib/reportFormat.fmtSex) accept the legacy and
the normalised value sets.

Data-only and reversible: no schema change, and ``downgrade()`` restores the DICOM codes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


# Case-insensitive so "m"/"M" both convert; the WHERE guard keeps it idempotent.
_TO_FHIR = (("M", "male"), ("F", "female"), ("O", "other"))


def upgrade() -> None:
    bind = op.get_bind()
    if "studies" not in set(sa.inspect(bind).get_table_names()):
        return

    converted = 0
    for dicom_code, fhir_code in _TO_FHIR:
        result = bind.execute(
            sa.text(
                "UPDATE studies SET patient_sex = :fhir "
                "WHERE patient_sex IS NOT NULL AND UPPER(TRIM(patient_sex)) = :dicom"
            ),
            {"fhir": fhir_code, "dicom": dicom_code},
        )
        converted += result.rowcount or 0

    remaining = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM studies WHERE patient_sex IS NOT NULL "
            "AND LOWER(TRIM(patient_sex)) NOT IN ('male', 'female', 'other', 'unknown')"
        )
    ).scalar_one()

    print(f"029: normalised {converted} studies.patient_sex value(s) to administrativeGender")
    if remaining:
        print(
            f"029: {remaining} row(s) hold an unrecognised patient_sex and were left as-is "
            "— inspect with: SELECT DISTINCT patient_sex FROM studies "
            "WHERE LOWER(TRIM(patient_sex)) NOT IN ('male','female','other','unknown')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "studies" not in set(sa.inspect(bind).get_table_names()):
        return

    for dicom_code, fhir_code in _TO_FHIR:
        bind.execute(
            sa.text(
                "UPDATE studies SET patient_sex = :dicom "
                "WHERE patient_sex IS NOT NULL AND LOWER(TRIM(patient_sex)) = :fhir"
            ),
            {"dicom": dicom_code, "fhir": fhir_code},
        )
    # "unknown" has no DICOM equivalent and was never written by the old code path, so
    # there is nothing to reverse for it.
