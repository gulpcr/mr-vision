"""Reading workflow: study lifecycle + assignment + TAT, and role permission re-sync.

Revision ID: 017
Revises: 016
Create Date: 2026-06-22

Adds the radiologist reading lifecycle to `studies` (reading_status, assignment,
and reported/signed timestamps; created_at is the received time for turnaround),
all additive/defaulted so existing studies become `unread`. Re-syncs the seeded
system roles to the current permission catalog (adds `study.claim`).
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studies",
        sa.Column("reading_status", sa.String(16), nullable=False, server_default="unread"),
    )
    op.add_column("studies", sa.Column("assigned_to", sa.String(36), nullable=True))
    op.add_column("studies", sa.Column("assigned_to_username", sa.String(128), nullable=True))
    op.add_column("studies", sa.Column("assigned_at", sa.DateTime(), nullable=True))
    op.add_column("studies", sa.Column("reported_at", sa.DateTime(), nullable=True))
    op.add_column("studies", sa.Column("signed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_studies_reading_status", "studies", ["reading_status"])
    op.create_index("ix_studies_assigned_to", "studies", ["assigned_to"])

    # Re-sync seeded system roles to the current catalog (idempotent).
    from app.domain.permissions import SYSTEM_ROLE_PERMISSIONS

    bind = op.get_bind()
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        bind.execute(
            sa.text(
                "UPDATE roles SET permissions = CAST(:perms AS JSON) "
                "WHERE name = :name AND is_system = true"
            ),
            {"perms": json.dumps(perms), "name": name},
        )


def downgrade() -> None:
    op.drop_index("ix_studies_assigned_to", table_name="studies")
    op.drop_index("ix_studies_reading_status", table_name="studies")
    for col in (
        "signed_at", "reported_at", "assigned_at",
        "assigned_to_username", "assigned_to", "reading_status",
    ):
        op.drop_column("studies", col)
