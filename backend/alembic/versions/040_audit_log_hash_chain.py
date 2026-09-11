"""Tamper-evident hash chain for audit_log.

Revision ID: 040
Revises: 039
Create Date: 2026-08-19

Adds seq/prev_hash/row_hash so each new audit entry is cryptographically
chained to the one before it (see app.domain.audit_chain and
PgAuditRepository.save()) — editing, deleting, or reordering a row breaks the
chain from that point forward, detectable by AuditIntegrityService.verify_chain().

Existing rows are left with seq/prev_hash/row_hash = NULL rather than
backfilled: the chain is a forward-looking integrity guarantee from the point
it was introduced, not a retroactive claim about data written before this
feature existed (which this migration cannot actually vouch for).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("seq", sa.Integer(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("row_hash", sa.String(64), nullable=True))
    op.create_index("ix_audit_log_seq", "audit_log", ["seq"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_audit_log_seq", "audit_log")
    op.drop_column("audit_log", "row_hash")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "seq")
