"""Login hardening (HIPAA 164.312(d)): forced password change and login bookkeeping.

Revision ID: 050
Revises: 049
Create Date: 2026-09-29

* ``users.must_change_password`` — while true the account can do nothing except change
  its password (enforced in the auth middleware). Set for seeded / admin-reset accounts.
* ``users.password_changed_at`` — when the password was last set by its owner.
* ``users.last_login_at`` — last successful sign-in (shown to admins for access reviews).

All three are additive with safe defaults; no data is rewritten.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "must_change_password")
