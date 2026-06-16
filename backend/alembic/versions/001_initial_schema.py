"""Initial schema for MRI AI Platform.

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "studies",
        sa.Column("study_instance_uid", sa.String(128), primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=True, index=True),
        sa.Column("patient_name", sa.String(256), nullable=True),
        sa.Column("study_date", sa.DateTime, nullable=True, index=True),
        sa.Column("study_description", sa.String(512), nullable=True),
        sa.Column("accession_number", sa.String(64), nullable=True, index=True),
        sa.Column("referring_physician", sa.String(256), nullable=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True, index=True),
        sa.Column("modality", sa.String(16), nullable=True, index=True),
        sa.Column("institution_name", sa.String(256), nullable=True),
        sa.Column("orthanc_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_studies_body_modality", "studies", ["body_part_examined", "modality"])

    op.create_table(
        "series",
        sa.Column("series_instance_uid", sa.String(128), primary_key=True),
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("series_number", sa.Integer, nullable=True),
        sa.Column("series_description", sa.String(512), nullable=True),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True),
        sa.Column("protocol_name", sa.String(256), nullable=True),
        sa.Column("num_instances", sa.Integer, default=0),
        sa.Column("slice_thickness", sa.Float, nullable=True),
        sa.Column("pixel_spacing", sa.JSON, nullable=True),
        sa.Column("image_orientation", sa.String(256), nullable=True),
        sa.Column("orthanc_id", sa.String(128), nullable=True),
        sa.Column("dicom_tags", sa.JSON, default=dict),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_series_study_uid", "series", ["study_instance_uid"])

    op.create_table(
        "job_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("usecase_name", sa.String(128), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, default="pending", index=True),
        sa.Column("priority", sa.Integer, default=0),
        sa.Column("progress", sa.Float, default=0.0),
        sa.Column("status_message", sa.Text, default=""),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_job_runs_study_usecase", "job_runs", ["study_instance_uid", "usecase_name"]
    )
    op.create_index("ix_job_runs_status_created", "job_runs", ["status", "created_at"])

    op.create_table(
        "results_index",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "study_instance_uid",
            sa.String(128),
            sa.ForeignKey("studies.study_instance_uid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("usecase_name", sa.String(128), nullable=False, index=True),
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("job_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("summary", sa.JSON, default=dict),
        sa.Column("measurements", sa.JSON, default=dict),
        sa.Column("qa_flags", sa.JSON, default=list),
        sa.Column("qa_details", sa.JSON, default=dict),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("model_checksum", sa.String(128), nullable=False),
        sa.Column("artifacts", sa.JSON, default=list),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_results_study_usecase",
        "results_index",
        ["study_instance_uid", "usecase_name"],
        unique=True,
    )

    op.create_table(
        "usecase_registry",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("supported_body_parts", sa.JSON, default=list),
        sa.Column("required_sequences", sa.JSON, default=list),
        sa.Column("model_type", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("module_path", sa.String(512), nullable=False),
        sa.Column("description", sa.Text, default=""),
        sa.Column("registered_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("actor", sa.String(128), default="system"),
        sa.Column("details", sa.JSON, default=dict),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now(), nullable=False, index=True),
    )
    op.create_index("ix_audit_entity", "audit_log", ["entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("usecase_registry")
    op.drop_table("results_index")
    op.drop_table("job_runs")
    op.drop_table("series")
    op.drop_table("studies")
