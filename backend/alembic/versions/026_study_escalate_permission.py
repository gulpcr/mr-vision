"""Grant study.escalate to existing technician/radiologist role rows.

Revision ID: 026
Revises: 025
Create Date: 2026-07-23

Migration 016 seeded the system roles once, as a one-time INSERT — it does not
reconcile with domain.permissions.SYSTEM_ROLE_PERMISSIONS on every startup. That
source-of-truth dict now grants technician and radiologist "study.escalate" (the
remote-site "escalate to an available radiologist" action, distinct from the
broader study.claim), so already-migrated databases need this row-level update
or their previously-seeded roles won't have it. Additive only — appends the key
if missing, never removes anything else.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None

_ROLES_TO_GRANT = ("technician", "radiologist")
_NEW_PERMISSION = "study.escalate"


def _load_role_rows(bind):
    return bind.execute(
        sa.text("SELECT id, permissions FROM roles WHERE name IN :names AND is_system = true").bindparams(
            sa.bindparam("names", expanding=True)
        ),
        {"names": list(_ROLES_TO_GRANT)},
    ).fetchall()


def upgrade() -> None:
    bind = op.get_bind()
    for row_id, perms in _load_role_rows(bind):
        current = perms if isinstance(perms, list) else json.loads(perms)
        if _NEW_PERMISSION not in current:
            updated = sorted(set(current) | {_NEW_PERMISSION})
            bind.execute(
                sa.text("UPDATE roles SET permissions = CAST(:perms AS JSON) WHERE id = :id"),
                {"perms": json.dumps(updated), "id": row_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    for row_id, perms in _load_role_rows(bind):
        current = perms if isinstance(perms, list) else json.loads(perms)
        if _NEW_PERMISSION in current:
            updated = [p for p in current if p != _NEW_PERMISSION]
            bind.execute(
                sa.text("UPDATE roles SET permissions = CAST(:perms AS JSON) WHERE id = :id"),
                {"perms": json.dumps(updated), "id": row_id},
            )
