from __future__ import annotations

"""Business associate agreements on file for customer tenants (checklist ADM-01).

The platform is the customer's business associate: no PHI may flow before a BAA is in
place. With REQUIRE_TENANT_BAA (production: on) a tenant without an active BAA record is
provisioned suspended, cannot be activated, and gets no DICOM endpoint or API key — the
two ways studies reach the platform. A record holds the counterparty, signatory, dates
and the SHA-256 of the signed document (the document itself stays in the compliance
repository). Records are never deleted; a terminated BAA is closed with a date.
"""

import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

import structlog
from sqlalchemy import or_, select

from app.config import get_settings

logger = structlog.get_logger(__name__)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BAARequired(Exception):
    """No active BAA is on file for the tenant."""


class InvalidBAA(ValueError):
    pass


class TenantBAAService:
    def __init__(self, session):
        self._session = session

    def _active(self, X, on: date):
        return (X.terminated_at.is_(None), X.effective_from <= on,
                or_(X.expires_on.is_(None), X.expires_on >= on))

    async def active_baa(self, tenant_id: str, on: date | None = None):
        from app.infrastructure.database.models import TenantBAARecord as X

        return (await self._session.execute(
            select(X).where(X.tenant_id == tenant_id, *self._active(X, on or date.today()))
            .order_by(X.effective_from.desc()).limit(1)
        )).scalar_one_or_none()

    async def require_active(self, tenant_id: str) -> None:
        """Raise BAARequired when REQUIRE_TENANT_BAA is on and no BAA is active."""
        if not get_settings().require_tenant_baa:
            return
        if await self.active_baa(tenant_id) is None:
            raise BAARequired(
                "No active business associate agreement is on file for this workspace; "
                "record the signed BAA first"
            )

    async def record(self, tenant_id: str, *, counterparty: str, signatory_name: str,
                     signatory_title: str | None, signed_on: date, effective_from: date,
                     expires_on: date | None, document_name: str, document_sha256: str,
                     actor: str) -> dict[str, Any]:
        from app.application.audit_service import AuditService
        from app.infrastructure.database.models import TenantBAARecord

        digest = (document_sha256 or "").strip().lower()
        if not _SHA256.match(digest):
            raise InvalidBAA("document_sha256 must be the 64-hex SHA-256 of the signed BAA")
        if signed_on > date.today():
            raise InvalidBAA("signed_on cannot be in the future")
        if expires_on is not None and expires_on <= effective_from:
            raise InvalidBAA("expires_on must be after effective_from")
        rec = TenantBAARecord(
            id=str(uuid.uuid4()), tenant_id=tenant_id, counterparty=counterparty.strip(),
            signatory_name=signatory_name.strip(), signatory_title=(signatory_title or "").strip() or None,
            signed_on=signed_on, effective_from=effective_from, expires_on=expires_on,
            document_name=document_name.strip(), document_sha256=digest, recorded_by=actor,
        )
        self._session.add(rec)
        await self._session.flush()
        await AuditService(self._session).record(
            "tenant_baa_recorded", "tenant_baa", rec.id, actor_display=actor,
            details={"tenant": tenant_id, "effective_from": effective_from.isoformat(),
                     "expires_on": expires_on.isoformat() if expires_on else None,
                     "document_sha256": digest},
            after=self._dict(rec),
        )
        logger.info("tenant_baa_recorded", tenant_id=tenant_id, baa_id=rec.id)
        return self._dict(rec)

    async def terminate(self, tenant_id: str, baa_id: str, actor: str) -> dict[str, Any]:
        from app.application.audit_service import AuditService
        from app.infrastructure.database.models import TenantBAARecord as X

        rec = (await self._session.execute(
            select(X).where(X.id == baa_id, X.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if rec is None:
            raise LookupError(baa_id)
        if rec.terminated_at is None:
            before = self._dict(rec)
            rec.terminated_at = datetime.now(timezone.utc)
            rec.terminated_by = actor
            await AuditService(self._session).record(
                "tenant_baa_terminated", "tenant_baa", rec.id, actor_display=actor,
                details={"tenant": tenant_id}, before=before, after=self._dict(rec),
            )
        return self._dict(rec)

    async def list(self, tenant_id: str) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import TenantBAARecord as X

        rows = (await self._session.execute(
            select(X).where(X.tenant_id == tenant_id).order_by(X.effective_from.desc())
        )).scalars().all()
        return [self._dict(r) for r in rows]

    @staticmethod
    def _dict(r) -> dict[str, Any]:
        today = date.today()
        return {
            "id": r.id, "tenant_id": r.tenant_id, "counterparty": r.counterparty,
            "signatory_name": r.signatory_name, "signatory_title": r.signatory_title,
            "signed_on": r.signed_on.isoformat(), "effective_from": r.effective_from.isoformat(),
            "expires_on": r.expires_on.isoformat() if r.expires_on else None,
            "document_name": r.document_name, "document_sha256": r.document_sha256,
            "recorded_by": r.recorded_by,
            "terminated_at": r.terminated_at.isoformat() if r.terminated_at else None,
            "active": (r.terminated_at is None and r.effective_from <= today
                       and (r.expires_on is None or r.expires_on >= today)),
        }
