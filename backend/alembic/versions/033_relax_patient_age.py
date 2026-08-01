"""Relax the patients.age_band check constraint.

Revision ID: 033
Revises: 032
Create Date: 2026-07-22

Patient intake now records an EXACT age (whole years) instead of a coarse band, so the
old CHECK that restricted ``age_band`` to the four band values ('0-17','18-39','40-64',
'65+') would reject a value like '11'. Drop it; the ``age_band`` column is retained (it
now holds an exact age string) and the application layer validates the value
(OnboardingService._normalize_age, 0-150). Existing rows carrying legacy band strings
remain valid — the column is unconstrained at the DB level after this.
"""
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_patients_age_band", "patients", type_="check")


def downgrade() -> None:
    # Recreate the band-only constraint. NOTE: this will fail if any row holds a
    # non-band age (e.g. an exact age recorded after the upgrade).
    op.create_check_constraint(
        "ck_patients_age_band",
        "patients",
        "age_band IN ('0-17','18-39','40-64','65+')",
    )
