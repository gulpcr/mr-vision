from __future__ import annotations

"""Periodic audit-log review (HIPAA 164.308(a)(1)(ii)(D), 164.312(b)).

The audit log records everything; this turns one tenant's period of it into the short
list a reviewer actually has to look at, keeps that summary, and records who reviewed it.
Generated monthly by Celery Beat (``run_audit_review_summary``) or on demand.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select

from app.config import get_settings
from app.domain.enums import AuditAction

logger = structlog.get_logger(__name__)

# Action groups shown in the summary (audit_log.action values).
FAILED_SIGN_IN = ("login_failed", "mfa_failed")
LOCKOUTS = ("account_locked",)
SESSION_ALARMS = ("session_reuse_detected",)
EMERGENCY_ACCESS = ("break_glass_invoked", "break_glass_revoked")
INTEGRITY_ALARMS = ("artifact_integrity_violation", "signed_document_integrity_failed")
DISCLOSURES = (
    "result_exported", "report_downloaded", "patient_record_disclosed", "share_link_redeemed",
    "share_link_created", "access_report_generated",
)
IMPERSONATION = ("impersonation_started", "impersonation_stopped")
# Detail keys safe and useful to show a reviewer (never free-form payloads).
_DETAIL_KEYS = ("reason", "patient_id", "route", "usecase", "signature_id", "expires_at")
TOP_N = 10


class AuditReviewNotFound(Exception):
    pass


def previous_month(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[first day of last month, first day of this month) in UTC."""
    now = now or datetime.now(timezone.utc)
    this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    return last_month, this_month


def previous_week(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[Monday of last week, Monday of this week) in UTC."""
    now = now or datetime.now(timezone.utc)
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return this_monday - timedelta(days=7), this_monday


def _business_hours() -> tuple[int, int]:
    try:
        start, end = get_settings().audit_review_business_hours.split("-", 1)
        return int(start), int(end)
    except ValueError:
        return 7, 19


class AuditReviewService:
    def __init__(self, session, tenant_id: str):
        self._session = session
        self._tenant_id = tenant_id

    def _in_period(self, start: datetime, end: datetime):
        from app.infrastructure.database.models import AuditLogRecord as A

        return and_(A.tenant_id == self._tenant_id, A.timestamp >= start, A.timestamp < end)

    async def _counts(self, start: datetime, end: datetime, actions: tuple[str, ...]) -> dict[str, int]:
        from app.infrastructure.database.models import AuditLogRecord as A

        rows = (await self._session.execute(
            select(A.action, func.count()).where(self._in_period(start, end), A.action.in_(actions))
            .group_by(A.action)
        )).all()
        return {action: int(n) for action, n in rows}

    async def _events(self, start: datetime, end: datetime, actions: tuple[str, ...],
                      limit: int = 50) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import AuditLogRecord as A

        rows = (await self._session.execute(
            select(A).where(self._in_period(start, end), A.action.in_(actions))
            .order_by(A.timestamp.desc()).limit(limit)
        )).scalars().all()
        return [{
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "action": r.action,
            "actor": r.actor_display or r.actor,
            "actor_id": r.actor_id,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "client_ip": r.client_ip,
            "details": {k: v for k, v in (r.details or {}).items() if k in _DETAIL_KEYS},
        } for r in rows]

    async def _top_readers(self, start: datetime, end: datetime, extra=None) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import AuditLogRecord as A

        who = func.coalesce(A.actor_display, A.actor)
        cond = [self._in_period(start, end), A.action == AuditAction.PHI_ACCESSED.value]
        if extra is not None:
            cond.append(extra)
        rows = (await self._session.execute(
            select(who, func.count()).where(*cond).group_by(who)
            .order_by(func.count().desc()).limit(TOP_N)
        )).all()
        return [{"key": k or "unknown", "count": int(n)} for k, n in rows]

    async def _scalar_count(self, *cond) -> int:
        from app.infrastructure.database.models import AuditLogRecord as A

        return int((await self._session.execute(
            select(func.count()).select_from(A).where(*cond)
        )).scalar_one())

    async def build_summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        from app.infrastructure.database.models import AuditLogRecord as A

        tz = get_settings().audit_review_timezone or "UTC"
        open_h, close_h = _business_hours()
        local = func.timezone(tz, A.timestamp)
        local_hour = func.extract("hour", local)
        after_hours = or_(local_hour < open_h, local_hour >= close_h,
                          func.extract("isodow", local) >= 6)
        period = self._in_period(start, end)
        phi = A.action == AuditAction.PHI_ACCESSED.value

        failed = await self._counts(start, end, FAILED_SIGN_IN)
        failed_ips = (await self._session.execute(
            select(A.client_ip, func.count()).where(period, A.action.in_(FAILED_SIGN_IN))
            .group_by(A.client_ip).order_by(func.count().desc()).limit(TOP_N)
        )).all()

        summary: dict[str, Any] = {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "timezone": tz,
            "business_hours": f"{open_h:02d}:00-{close_h:02d}:00 Mon-Fri",
            "total_events": await self._scalar_count(period),
            "failed_sign_ins": {
                "by_action": failed,
                "total": sum(failed.values()),
                "top_client_ips": [{"key": ip or "unknown", "count": int(n)} for ip, n in failed_ips],
            },
            "lockouts": await self._events(start, end, LOCKOUTS),
            "session_alarms": await self._events(start, end, SESSION_ALARMS),
            "emergency_access": await self._events(start, end, EMERGENCY_ACCESS),
            "integrity_alarms": await self._events(start, end, INTEGRITY_ALARMS),
            "disclosures": await self._counts(start, end, DISCLOSURES),
            "impersonations": await self._events(start, end, IMPERSONATION),
            "phi_reads": await self._scalar_count(period, phi),
            "after_hours_phi_reads": await self._scalar_count(period, phi, after_hours),
            "top_readers": await self._top_readers(start, end),
            "top_after_hours_readers": await self._top_readers(start, end, after_hours),
        }
        summary["findings"] = (
            len(summary["lockouts"]) + len(summary["session_alarms"])
            + sum(1 for e in summary["emergency_access"] if e["action"] == "break_glass_invoked")
            + len(summary["integrity_alarms"])
        )
        return summary

    async def generate(self, start: datetime, end: datetime, actor: str = "system") -> dict[str, Any]:
        """Create (or return the existing) review for this tenant and period."""
        from app.application.audit_service import AuditService
        from app.infrastructure.database.models import AuditReviewRecord

        existing = (await self._session.execute(
            select(AuditReviewRecord).where(
                AuditReviewRecord.tenant_id == self._tenant_id,
                AuditReviewRecord.period_start == start,
            )
        )).scalar_one_or_none()
        if existing is not None:
            return self._dict(existing)
        summary = await self.build_summary(start, end)
        rec = AuditReviewRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id, period_start=start, period_end=end,
            summary=summary, findings_count=summary["findings"],
        )
        self._session.add(rec)
        await self._session.flush()
        await AuditService(self._session).record(
            AuditAction.AUDIT_REVIEW_GENERATED.value, "audit_review", rec.id,
            actor_display=actor,
            details={"period_start": start.isoformat(), "findings": summary["findings"]},
        )
        logger.info("audit_review_generated", tenant_id=self._tenant_id,
                    period_start=start.isoformat(), findings=summary["findings"])
        return self._dict(rec)

    async def list(self) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import AuditReviewRecord

        rows = (await self._session.execute(
            select(AuditReviewRecord).where(AuditReviewRecord.tenant_id == self._tenant_id)
            .order_by(AuditReviewRecord.period_start.desc())
        )).scalars().all()
        return [self._dict(r, with_summary=False) for r in rows]

    async def get(self, review_id: str) -> dict[str, Any]:
        return self._dict(await self._record(review_id))

    async def mark_reviewed(self, review_id: str, user_id: str, username: str, notes: str,
                            client_ip: str | None = None) -> dict[str, Any]:
        from app.application.audit_service import AuditService

        rec = await self._record(review_id)
        if rec.reviewed_at is not None:
            return self._dict(rec)
        rec.reviewed_by = user_id or None
        rec.reviewed_by_username = username
        rec.reviewed_at = datetime.now(timezone.utc)
        rec.review_notes = (notes or "").strip()[:8000] or None
        await AuditService(self._session).record(
            AuditAction.AUDIT_REVIEWED.value, "audit_review", rec.id,
            actor_id=user_id, actor_display=username, client_ip=client_ip,
            details={"period_start": rec.period_start.isoformat(),
                     "findings": rec.findings_count, "has_notes": bool(rec.review_notes)},
        )
        return self._dict(rec)

    async def _record(self, review_id: str):
        from app.infrastructure.database.models import AuditReviewRecord

        rec = (await self._session.execute(
            select(AuditReviewRecord).where(
                AuditReviewRecord.id == review_id, AuditReviewRecord.tenant_id == self._tenant_id,
            )
        )).scalar_one_or_none()
        if rec is None:
            raise AuditReviewNotFound(review_id)
        return rec

    @staticmethod
    def _dict(r, with_summary: bool = True) -> dict[str, Any]:
        out = {
            "id": r.id,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "findings_count": r.findings_count,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "reviewed_by_username": r.reviewed_by_username,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "review_notes": r.review_notes,
        }
        if with_summary:
            out["summary"] = r.summary
        return out
