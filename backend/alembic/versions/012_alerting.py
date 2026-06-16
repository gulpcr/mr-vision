"""Alert rules and history tables.

Revision ID: 012
Revises: 011
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("condition", sa.JSON(), default=dict),
        sa.Column("webhook_url", sa.String(1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("tenant_id", sa.String(36), default="default"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_alert_rules_event_type", "alert_rules", ["event_type"])

    op.create_table(
        "alert_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), default=dict),
        sa.Column("status", sa.String(32), default="sent"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_alert_history_created", "alert_history", ["created_at"])


def downgrade() -> None:
    op.drop_table("alert_history")
    op.drop_table("alert_rules")
