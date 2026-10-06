from __future__ import annotations

"""Patient-request register and 164.522 restrictions (checklist PRV-04).

* Requests: every individual-rights request is logged with its received date and the
  statutory due date (domain/patient_rights.py); one documented extension; closed as
  fulfilled or denied with notes. Overdue requests are flagged.
* Restrictions: an agreed restriction blocks the named outbound channel for that MRN —
  FHIR push, DICOM SR/SEG export, webhooks, share links, or all of them —
  until it expires or is revoked. ``is_restricted`` is asked by every outbound path
  (export_gate, alerting_service, portal_service).

Every change is recorded in the hash-chained audit log with before/after state hashes.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import or_, select

from app.domain.patient_rights import (
    CLOSED_STATUSES,
    OPEN_STATUSES,
    RESTRICTION_CHANNELS,
    InvalidRequest,
    due_date,
    extended_due_date,
    is_overdue,
)

logger = structlog.get_logger(__name__)


class RequestNotFound(LookupError):
    pass


class DisclosureRestricted(Exception):
    """An agreed 164.522 restriction forbids this disclosure."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


class PatientRightsService:
    def __init__(self, session, tenant_id: str):
        self._session = session
        self._tenant_id = tenant_id

    async def _audit(self, action: str, entity_type: str, entity_id: str, actor: str,
                     details: dict, before=None, after=None) -> None:
        from app.application.audit_service import AuditService

        await AuditService(self._session).record(
            action, entity_type, entity_id, actor_display=actor, details=details,
            before=before, after=after,
        )

    # ── requests ──────────────────────────────────────────────────────────────

    async def create_request(self, mrn: str, request_type: str, requester: str, actor: str,
                             details: str | None = None,
                             received_at: datetime | None = None) -> dict[str, Any]:
        from app.infrastructure.database.models import PatientRequestRecord

        mrn = (mrn or "").strip()
        if not mrn:
            raise InvalidRequest("MRN is required")
        received = received_at or _now()
        if received > _now():
            raise InvalidRequest("The received date cannot be in the future")
        rec = PatientRequestRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id, mrn=mrn, request_type=request_type,
            requester=requester.strip(), details=(details or "").strip()[:8000] or None,
            received_at=received, due_at=due_date(request_type, received), status="open",
            created_by=actor,
        )
        self._session.add(rec)
        await self._session.flush()
        await self._audit("patient_request_logged", "patient_request", rec.id, actor,
                          {"patient_id": mrn, "type": request_type, "due_at": rec.due_at.isoformat()},
                          after=self._dict(rec))
        return self._dict(rec)

    async def list_requests(self, status: str | None = None, mrn: str | None = None) -> list[dict]:
        from app.infrastructure.database.models import PatientRequestRecord as R

        stmt = select(R).where(R.tenant_id == self._tenant_id)
        if status == "open":
            stmt = stmt.where(R.status.in_(OPEN_STATUSES))
        elif status == "overdue":
            stmt = stmt.where(R.status.in_(OPEN_STATUSES), R.due_at < _now())
        elif status:
            stmt = stmt.where(R.status == status)
        if mrn:
            stmt = stmt.where(R.mrn == mrn)
        rows = (await self._session.execute(stmt.order_by(R.due_at.asc()))).scalars().all()
        return [self._dict(r) for r in rows]

    async def extend_request(self, request_id: str, reason: str, actor: str) -> dict[str, Any]:
        rec = await self._request(request_id)
        if rec.status != "open":
            raise InvalidRequest("Only an open request can be extended, and only once")
        if _now() > rec.due_at:
            raise InvalidRequest("The extension must be made before the original due date")
        if not (reason or "").strip():
            raise InvalidRequest("The reason for the delay is required (it must be given to the patient)")
        before = self._dict(rec)
        rec.status = "extended"
        rec.extended_at = _now()
        rec.extension_reason = reason.strip()[:4000]
        rec.due_at = extended_due_date(rec.request_type, rec.received_at)
        await self._audit("patient_request_extended", "patient_request", rec.id, actor,
                          {"patient_id": rec.mrn, "due_at": rec.due_at.isoformat()},
                          before=before, after=self._dict(rec))
        return self._dict(rec)

    async def close_request(self, request_id: str, outcome: str, notes: str, actor: str) -> dict[str, Any]:
        if outcome not in CLOSED_STATUSES:
            raise InvalidRequest(f"Outcome must be one of {', '.join(CLOSED_STATUSES)}")
        rec = await self._request(request_id)
        if rec.status in CLOSED_STATUSES:
            raise InvalidRequest("The request is already closed")
        if outcome == "denied" and not (notes or "").strip():
            raise InvalidRequest("A denial needs the reason (the patient must be told why)")
        before = self._dict(rec)
        rec.status = outcome
        rec.closed_at = _now()
        rec.closed_by = actor
        rec.outcome_notes = (notes or "").strip()[:8000] or None
        await self._audit("patient_request_closed", "patient_request", rec.id, actor,
                          {"patient_id": rec.mrn, "outcome": outcome,
                           "on_time": rec.closed_at <= rec.due_at},
                          before=before, after=self._dict(rec))
        return self._dict(rec)

    async def _request(self, request_id: str):
        from app.infrastructure.database.models import PatientRequestRecord as R

        rec = (await self._session.execute(
            select(R).where(R.id == request_id, R.tenant_id == self._tenant_id)
        )).scalar_one_or_none()
        if rec is None:
            raise RequestNotFound(request_id)
        return rec

    @staticmethod
    def _dict(r) -> dict[str, Any]:
        return {
            "id": r.id, "mrn": r.mrn, "request_type": r.request_type, "requester": r.requester,
            "details": r.details, "received_at": _iso(r.received_at), "due_at": _iso(r.due_at),
            "extended_at": _iso(r.extended_at), "extension_reason": r.extension_reason,
            "status": r.status, "closed_at": _iso(r.closed_at), "closed_by": r.closed_by,
            "outcome_notes": r.outcome_notes, "created_by": r.created_by,
            "overdue": is_overdue(r.status, r.due_at, _now()) if r.due_at else False,
        }

    # ── restrictions ──────────────────────────────────────────────────────────

    async def add_restriction(self, mrn: str, channel: str, reason: str, actor: str,
                              expires_at: datetime | None = None,
                              request_id: str | None = None) -> dict[str, Any]:
        from app.infrastructure.database.models import PatientRestrictionRecord

        if channel not in RESTRICTION_CHANNELS:
            raise InvalidRequest(f"Channel must be one of {', '.join(RESTRICTION_CHANNELS)}")
        if not (mrn or "").strip() or not (reason or "").strip():
            raise InvalidRequest("MRN and reason are required")
        if request_id:
            await self._request(request_id)
        rec = PatientRestrictionRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id, mrn=mrn.strip(), channel=channel,
            reason=reason.strip()[:4000], request_id=request_id, agreed_by=actor,
            agreed_at=_now(), expires_at=expires_at,
        )
        self._session.add(rec)
        await self._session.flush()
        await self._audit("patient_restriction_agreed", "patient_restriction", rec.id, actor,
                          {"patient_id": rec.mrn, "channel": channel}, after=self._rdict(rec))
        return self._rdict(rec)

    async def revoke_restriction(self, restriction_id: str, actor: str) -> dict[str, Any]:
        from app.infrastructure.database.models import PatientRestrictionRecord as X

        rec = (await self._session.execute(
            select(X).where(X.id == restriction_id, X.tenant_id == self._tenant_id)
        )).scalar_one_or_none()
        if rec is None:
            raise RequestNotFound(restriction_id)
        if rec.revoked_at is None:
            before = self._rdict(rec)
            rec.revoked_at = _now()
            rec.revoked_by = actor
            await self._audit("patient_restriction_revoked", "patient_restriction", rec.id, actor,
                              {"patient_id": rec.mrn, "channel": rec.channel},
                              before=before, after=self._rdict(rec))
        return self._rdict(rec)

    async def list_restrictions(self, mrn: str | None = None, active_only: bool = False) -> list[dict]:
        from app.infrastructure.database.models import PatientRestrictionRecord as X

        stmt = select(X).where(X.tenant_id == self._tenant_id)
        if mrn:
            stmt = stmt.where(X.mrn == mrn)
        if active_only:
            stmt = stmt.where(*self._active(X))
        rows = (await self._session.execute(stmt.order_by(X.agreed_at.desc()))).scalars().all()
        return [self._rdict(r) for r in rows]

    @staticmethod
    def _active(X):
        return (X.revoked_at.is_(None), or_(X.expires_at.is_(None), X.expires_at > _now()))

    async def is_restricted(self, mrn: str | None, channel: str) -> bool:
        from app.infrastructure.database.models import PatientRestrictionRecord as X

        if not mrn:
            return False
        hit = (await self._session.execute(
            select(X.id).where(X.tenant_id == self._tenant_id, X.mrn == mrn,
                               X.channel.in_((channel, "all")), *self._active(X)).limit(1)
        )).first()
        return hit is not None

    async def study_restricted(self, study_uid: str, channel: str) -> bool:
        """Whether the study's patient has an active restriction on ``channel``."""
        from app.infrastructure.database.models import StudyRecord

        mrn = (await self._session.execute(
            select(StudyRecord.patient_id).where(StudyRecord.study_instance_uid == study_uid)
        )).scalar_one_or_none()
        return await self.is_restricted(mrn, channel)

    async def require_unrestricted(self, study_uid: str, channel: str, actor: str = "system") -> None:
        if await self.study_restricted(study_uid, channel):
            await self._audit("disclosure_blocked_by_restriction", "study", study_uid, actor,
                              {"channel": channel})
            logger.info("disclosure_blocked_by_restriction", study_uid=study_uid, channel=channel)
            raise DisclosureRestricted(
                "The patient has an agreed restriction on this disclosure (HIPAA 164.522); "
                "it cannot be sent"
            )

    @staticmethod
    def _rdict(r) -> dict[str, Any]:
        now = _now()
        return {
            "id": r.id, "mrn": r.mrn, "channel": r.channel, "reason": r.reason,
            "request_id": r.request_id, "agreed_by": r.agreed_by, "agreed_at": _iso(r.agreed_at),
            "expires_at": _iso(r.expires_at), "revoked_at": _iso(r.revoked_at),
            "revoked_by": r.revoked_by,
            "active": r.revoked_at is None and (r.expires_at is None or r.expires_at > now),
        }
