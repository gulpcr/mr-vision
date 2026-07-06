"""Structured per-breast finding slots on mammography_reports.

Revision ID: 025
Revises: 024
Create Date: 2026-07-01

Decomposes the free-text mammography findings into a fixed per-breast checklist
(density, mass, calcification, skin thickening, nipple retraction, architectural
distortion, axillary nodes). Pre-filled from the AI result (Mammo-CLIP density +
mass/calcification presence; radiologist-only slots default to their negative) and
editable by the radiologist. The existing *_breast_findings free-text columns remain
and now hold the narrative rendered FROM these slots. Additive — nothing existing is
altered or dropped.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

_TABLE = "mammography_reports"

# (column, sqlalchemy type, allowed non-null values) for the structured slots.
_DENSITY = ("a", "b", "c", "d")
_PRESENCE = ("none", "present")
_NODES = ("normal", "abnormal")

_SLOTS: list[tuple[str, int, tuple[str, ...]]] = []
for _side in ("right", "left"):
    _SLOTS.append((f"density_{_side}", 1, _DENSITY))
    _SLOTS.append((f"mass_{_side}", 16, _PRESENCE))
    _SLOTS.append((f"calcification_{_side}", 16, _PRESENCE))
    _SLOTS.append((f"skin_thickening_{_side}", 16, _PRESENCE))
    _SLOTS.append((f"nipple_retraction_{_side}", 16, _PRESENCE))
    _SLOTS.append((f"architectural_distortion_{_side}", 16, _PRESENCE))
    _SLOTS.append((f"axillary_nodes_{_side}", 32, _NODES))


def _in_clause(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    for col, length, values in _SLOTS:
        op.add_column(_TABLE, sa.Column(col, sa.String(length), nullable=True))
        op.create_check_constraint(
            f"ck_mammo_{col}",
            _TABLE,
            f"{col} IS NULL OR {col} IN {_in_clause(values)}",
        )


def downgrade() -> None:
    for col, _length, _values in _SLOTS:
        op.drop_constraint(f"ck_mammo_{col}", _TABLE, type_="check")
        op.drop_column(_TABLE, col)
