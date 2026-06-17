"""Add patient demographics columns to studies for reporting.

Adds patient_sex, patient_age, patient_weight_kg, patient_height_cm to the
studies table. Populated at ingest from DICOM PatientSex / PatientAge /
PatientWeight / PatientSize tags; used by the FDG PET-CT report layout.

Revision ID: 015
Revises: 014
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("studies", sa.Column("patient_sex", sa.String(16), nullable=True))
    op.add_column("studies", sa.Column("patient_age", sa.String(16), nullable=True))
    op.add_column("studies", sa.Column("patient_weight_kg", sa.Float, nullable=True))
    op.add_column("studies", sa.Column("patient_height_cm", sa.Float, nullable=True))


def downgrade() -> None:
    op.drop_column("studies", "patient_height_cm")
    op.drop_column("studies", "patient_weight_kg")
    op.drop_column("studies", "patient_age")
    op.drop_column("studies", "patient_sex")
