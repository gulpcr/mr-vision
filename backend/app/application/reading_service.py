"""Reading-workflow service: study lifecycle, assignment, and turnaround.

Lifecycle:  unread → in_progress → reported → signed
  - claim / assign / auto_assign : * → in_progress  (sets assignee)
  - unclaim                      : in_progress → unread (clears assignee)
  - report                       : in_progress → reported
  - sign                         : reported → signed

Authority (enforced here, in addition to the endpoint permission):
  - report / sign require the actor to be the assigned radiologist OR an admin.
Application layer — no FastAPI imports; the router maps exceptions to HTTP codes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, select

logger = structlog.get_logger(__name__)

UNREAD = "unread"
IN_PROGRESS = "in_progress"
REPORTED = "reported"
SIGNED = "signed"
VALID_STATUSES = (UNREAD, IN_PROGRESS, REPORTED, SIGNED)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StudyNotFoundError(Exception):
    """Study UID not found (→ 404)."""


class InvalidTransitionError(Exception):
    """Requested lifecycle transition is not allowed from the current state (→ 409)."""


class NotAssignedError(Exception):
    """Actor is neither the assigned radiologist nor an admin (→ 403)."""


class NoRadiologistError(Exception):
    """Auto-assign found no active radiologist (→ 409)."""


class ReadingService:
    def __init__(self, session):
        self._session = session

    async def _get(self, study_uid: str):
        from app.infrastructure.database.models import StudyRecord

        res = await self._session.execute(
            select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
        )
        rec = res.scalar_one_or_none()
        if rec is None:
            raise StudyNotFoundError(study_uid)
        return rec

    # ── Assignment ─────────────────────────────────────────────────────────────

    async def claim(self, study_uid: str, user_id: str, username: str) -> dict[str, Any]:
        """Radiologist self-claims an unread (or own) study → in_progress."""
        rec = await self._get(study_uid)
        if rec.reading_status not in (UNREAD, IN_PROGRESS):
            raise InvalidTransitionError(f"Cannot claim a '{rec.reading_status}' study")
        if rec.reading_status == IN_PROGRESS and rec.assigned_to not in (None, user_id):
            raise InvalidTransitionError(
                f"Already assigned to {rec.assigned_to_username or rec.assigned_to}"
            )
        return await self._assign_to(rec, user_id, username, actor=username, action="study_claimed")

    async def assign(
        self, study_uid: str, assignee_id: str, assignee_username: str, actor: str
    ) -> dict[str, Any]:
        """Assign/reassign a study to a radiologist (not allowed once signed)."""
        rec = await self._get(study_uid)
        if rec.reading_status == SIGNED:
            raise InvalidTransitionError("Cannot reassign a signed study")
        return await self._assign_to(rec, assignee_id, assignee_username, actor=actor, action="study_assigned")

    async def auto_assign(self, study_uid: str, actor: str, tenant_id: str = "default") -> dict[str, Any]:
        """Load-balance: assign to the active radiologist with the fewest
        in-progress studies (ties broken by username for determinism)."""
        from app.infrastructure.database.models import StudyRecord, UserRecord

        rec = await self._get(study_uid)
        if rec.reading_status == SIGNED:
            raise InvalidTransitionError("Cannot reassign a signed study")

        rads = (await self._session.execute(
            select(UserRecord).where(
                UserRecord.role == "radiologist",
                UserRecord.is_active == True,  # noqa: E712
                UserRecord.tenant_id == tenant_id,
            )
        )).scalars().all()
        if not rads:
            raise NoRadiologistError("No active radiologist to assign to")

        load_rows = (await self._session.execute(
            select(StudyRecord.assigned_to, func.count())
            .where(StudyRecord.reading_status == IN_PROGRESS)
            .group_by(StudyRecord.assigned_to)
        )).all()
        load = {uid: n for uid, n in load_rows}
        chosen = min(rads, key=lambda u: (load.get(u.id, 0), u.username))
        return await self._assign_to(
            rec, chosen.id, chosen.username, actor=actor, action="study_auto_assigned"
        )

    async def unclaim(self, study_uid: str, actor_id: str, actor: str, is_admin: bool) -> dict[str, Any]:
        """Release an in-progress study back to unread (assignee or admin)."""
        rec = await self._get(study_uid)
        if rec.reading_status != IN_PROGRESS:
            raise InvalidTransitionError(f"Cannot unclaim a '{rec.reading_status}' study")
        self._require_assignee_or_admin(rec, actor_id, is_admin)
        rec.reading_status = UNREAD
        rec.assigned_to = None
        rec.assigned_to_username = None
        rec.assigned_at = None
        await self._audit(actor, "study_unclaimed", rec.study_instance_uid, {})
        await self._session.flush()
        return self.reading_dict(rec)

    async def _assign_to(self, rec, user_id, username, actor, action) -> dict[str, Any]:
        rec.assigned_to = user_id
        rec.assigned_to_username = username
        rec.assigned_at = _now()
        if rec.reading_status == UNREAD:
            rec.reading_status = IN_PROGRESS
        await self._audit(actor, action, rec.study_instance_uid, {"assignee": username})
        await self._session.flush()
        return self.reading_dict(rec)

    # ── Reporting / signing ──────────────────────────────────────────────────

    async def report(self, study_uid: str, actor_id: str, actor: str, is_admin: bool) -> dict[str, Any]:
        rec = await self._get(study_uid)
        if rec.reading_status != IN_PROGRESS:
            raise InvalidTransitionError(f"Cannot report a '{rec.reading_status}' study (must be in_progress)")
        self._require_assignee_or_admin(rec, actor_id, is_admin)
        rec.reading_status = REPORTED
        rec.reported_at = _now()
        await self._audit(actor, "study_reported", rec.study_instance_uid, {})
        await self._session.flush()
        return self.reading_dict(rec)

    async def sign(self, study_uid: str, actor_id: str, actor: str, is_admin: bool) -> dict[str, Any]:
        rec = await self._get(study_uid)
        if rec.reading_status != REPORTED:
            raise InvalidTransitionError(f"Cannot sign a '{rec.reading_status}' study (must be reported)")
        self._require_assignee_or_admin(rec, actor_id, is_admin)
        rec.reading_status = SIGNED
        rec.signed_at = _now()
        await self._audit(actor, "study_signed", rec.study_instance_uid, {})
        await self._session.flush()
        return self.reading_dict(rec)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _require_assignee_or_admin(rec, actor_id: str, is_admin: bool) -> None:
        if is_admin:
            return
        if rec.assigned_to and rec.assigned_to == actor_id:
            return
        raise NotAssignedError("Only the assigned radiologist (or an admin) may do this")

    async def _audit(self, actor: str, action: str, entity_id: str, details: dict) -> None:
        from app.infrastructure.database.models import AuditLogRecord

        self._session.add(AuditLogRecord(
            id=str(uuid.uuid4()),
            action=action,
            entity_type="study_reading",
            entity_id=entity_id,
            actor=actor or "system",
            details=details,
        ))

    @staticmethod
    def reading_dict(rec) -> dict[str, Any]:
        """Reading-workflow fields + derived turnaround times (minutes)."""
        def mins(later):
            if not later or not rec.created_at:
                return None
            return round((later - rec.created_at).total_seconds() / 60.0, 1)

        return {
            "study_instance_uid": rec.study_instance_uid,
            "reading_status": rec.reading_status,
            "assigned_to": rec.assigned_to,
            "assigned_to_username": rec.assigned_to_username,
            "assigned_at": rec.assigned_at.isoformat() if rec.assigned_at else None,
            "reported_at": rec.reported_at.isoformat() if rec.reported_at else None,
            "signed_at": rec.signed_at.isoformat() if rec.signed_at else None,
            "tat_report_minutes": mins(rec.reported_at),
            "tat_signoff_minutes": mins(rec.signed_at),
        }
