"""Add critical_alerts table for clinical finding tracking.

Revision ID: 014
Revises: 013
Create Date: 2026-05-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "critical_alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False),
        sa.Column("usecase_name", sa.String(128), nullable=False),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("patient_id", sa.String(64), nullable=True),
        sa.Column("finding_type", sa.String(128), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details", sa.JSON, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("notification_channels", sa.JSON, nullable=True),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("acknowledged_by", sa.String(128), nullable=True),
        sa.Column("escalated_at", sa.DateTime, nullable=True),
        sa.Column("escalation_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_index("ix_critical_alerts_study_uid", "critical_alerts", ["study_instance_uid"])
    op.create_index("ix_critical_alerts_usecase", "critical_alerts", ["usecase_name"])
    op.create_index("ix_critical_alerts_result_id", "critical_alerts", ["result_id"])
    op.create_index("ix_critical_alerts_patient_id", "critical_alerts", ["patient_id"])
    op.create_index("ix_critical_alerts_status", "critical_alerts", ["status"])
    op.create_index("ix_critical_alerts_created_at", "critical_alerts", ["created_at"])
    op.create_index("ix_critical_alerts_status_severity", "critical_alerts", ["status", "severity"])
    op.create_index("ix_critical_alerts_study_usecase", "critical_alerts", ["study_instance_uid", "usecase_name"])


def downgrade() -> None:
    op.drop_table("critical_alerts")
