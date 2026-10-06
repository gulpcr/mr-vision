from __future__ import annotations

"""Break-glass emergency access (HIPAA 164.312(a)(2)(ii) emergency access procedure).

A referral-scoped user (the referring Doctor) may, in an emergency, open one patient's
studies outside their referrals for ``break_glass_minutes``, stating why. Every grant is
audited and pushed to the workspace in real time; administrators review and can revoke
it (``break_glass.review``). Every read made under a grant is audited like any other
read (phi_accessed), so the review can see exactly what was opened.

Visibility itself is enforced in the database (``studies.referral_scope`` policy,
alembic 053) and in the application filters (infrastructure/database/access_scope.py).
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select

from app.config import get_settings
from app.domain.enums import AuditAction
from app.domain.models import AuditEntry

logger = structlog.get_logger(__name__)


class BreakGlassError(ValueError):
    """The request cannot be granted (message is shown to the user)."""


def _row(g) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires = g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=timezone.utc)
    return {
        "id": g.id,
        "user_id": g.user_id,
        "username": g.username,
        "patient_id": g.patient_id,
        "reason": g.reason,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "expires_at": expires.isoformat(),
        "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
        "revoked_by": g.revoked_by,
        "active": g.revoked_at is None and expires > now,
    }


class BreakGlassService:
    def __init__(self, session, tenant_id: str):
        self._session = session
        self._tenant_id = tenant_id

    async def invoke(
        self, *, user_id: str, username: str, patient_id: str, reason: str, client_ip: str = "",
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import BreakGlassGrantRecord, StudyRecord
        from app.infrastructure.database.repositories import PgAuditRepository
        from app.infrastructure.database.session import async_session_factory
        from app.infrastructure.tenant.db_scope import tenant_scope

        settings = get_settings()
        patient_id = (patient_id or "").strip()
        reason = (reason or "").strip()
        if not patient_id:
            raise BreakGlassError("Enter the patient's MRN")
        if len(reason) < settings.break_glass_min_reason_chars:
            raise BreakGlassError(
                f"Describe the emergency (at least {settings.break_glass_min_reason_chars} characters)"
            )

        # The caller's own session hides non-referred studies (that is the point), so
        # existence is checked in a workspace-wide (tenant, not referral) scope.
        with tenant_scope(self._tenant_id):
            async with async_session_factory() as check:
                studies = (
                    await check.execute(
                        select(func.count()).select_from(StudyRecord).where(
                            StudyRecord.tenant_id == self._tenant_id,
                            StudyRecord.patient_id == patient_id,
                        )
                    )
                ).scalar_one()
        if not studies:
            raise BreakGlassError("No studies for this MRN in your workspace")

        now = datetime.now(timezone.utc)
        existing = (
            await self._session.execute(
                select(BreakGlassGrantRecord).where(
                    BreakGlassGrantRecord.tenant_id == self._tenant_id,
                    BreakGlassGrantRecord.user_id == user_id,
                    BreakGlassGrantRecord.patient_id == patient_id,
                    BreakGlassGrantRecord.revoked_at.is_(None),
                    BreakGlassGrantRecord.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _row(existing)

        grant = BreakGlassGrantRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id, user_id=user_id, username=username,
            patient_id=patient_id, reason=reason[:4000],
            expires_at=now + timedelta(minutes=settings.break_glass_minutes),
            client_ip=(client_ip or "")[:64] or None,
        )
        self._session.add(grant)
        await self._session.flush()
        await PgAuditRepository(self._session, tenant_id=self._tenant_id).save(AuditEntry(
            action=AuditAction.BREAK_GLASS_INVOKED, entity_type="patient", entity_id=patient_id,
            actor=username or user_id,
            details={"grant_id": grant.id, "reason": grant.reason, "studies": int(studies),
                     "expires_at": grant.expires_at.isoformat(), "client_ip": client_ip},
            tenant_id=self._tenant_id,
        ))
        await self._notify("break_glass_invoked", grant)
        logger.warning("break_glass_invoked", user=username, patient=patient_id, grant=grant.id)
        return _row(grant)

    async def mine(self, user_id: str) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import BreakGlassGrantRecord

        rows = (
            await self._session.execute(
                select(BreakGlassGrantRecord).where(
                    BreakGlassGrantRecord.tenant_id == self._tenant_id,
                    BreakGlassGrantRecord.user_id == user_id,
                ).order_by(BreakGlassGrantRecord.created_at.desc()).limit(50)
            )
        ).scalars().all()
        return [_row(g) for g in rows]

    async def list(self, limit: int = 200) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import BreakGlassGrantRecord

        rows = (
            await self._session.execute(
                select(BreakGlassGrantRecord).where(BreakGlassGrantRecord.tenant_id == self._tenant_id)
                .order_by(BreakGlassGrantRecord.created_at.desc()).limit(limit)
            )
        ).scalars().all()
        return [_row(g) for g in rows]

    async def revoke(self, grant_id: str, actor: str) -> dict[str, Any] | None:
        from app.infrastructure.database.models import BreakGlassGrantRecord
        from app.infrastructure.database.repositories import PgAuditRepository

        grant = (
            await self._session.execute(
                select(BreakGlassGrantRecord).where(
                    BreakGlassGrantRecord.id == grant_id,
                    BreakGlassGrantRecord.tenant_id == self._tenant_id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            return None
        if grant.revoked_at is None:
            grant.revoked_at = datetime.now(timezone.utc)
            grant.revoked_by = actor
            await self._session.flush()
            await PgAuditRepository(self._session, tenant_id=self._tenant_id).save(AuditEntry(
                action=AuditAction.BREAK_GLASS_REVOKED, entity_type="patient",
                entity_id=grant.patient_id, actor=actor,
                details={"grant_id": grant.id, "granted_to": grant.username},
                tenant_id=self._tenant_id,
            ))
            await self._notify("break_glass_revoked", grant)
        return _row(grant)

    async def _notify(self, event: str, grant) -> None:
        from app.infrastructure.realtime.events import publish_tenant_event

        await publish_tenant_event(self._tenant_id, {
            "type": event, "grant_id": grant.id, "username": grant.username,
            "patient_id": grant.patient_id, "expires_at": grant.expires_at.isoformat(),
        })
