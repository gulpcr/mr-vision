from __future__ import annotations

"""Row-visibility clauses shared by every application-level referral filter.

The referring Doctor sees a study when they referred it, or — break-glass emergency
access (alembic 053) — when they hold an active, unrevoked grant for its patient. The
database enforces the same rule in the ``studies.referral_scope`` RLS policy; these
clauses keep explicit application filters consistent with it.
"""

from sqlalchemy import func, or_, select

from app.infrastructure.database.models import BreakGlassGrantRecord, StudyRecord


def active_grant_patients(user_id: str):
    """Sub-select of MRNs the user holds an active break-glass grant for."""
    return select(BreakGlassGrantRecord.patient_id).where(
        BreakGlassGrantRecord.user_id == user_id,
        BreakGlassGrantRecord.revoked_at.is_(None),
        BreakGlassGrantRecord.expires_at > func.now(),
    )


def referral_visible(referring_user_id: str):
    """WHERE clause on StudyRecord for a referral-scoped user."""
    return or_(
        StudyRecord.referring_user_id == referring_user_id,
        StudyRecord.patient_id.in_(active_grant_patients(referring_user_id)),
    )
