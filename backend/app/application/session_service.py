from __future__ import annotations

"""Refresh sessions: short access tokens, a rotating refresh token, automatic logoff.

HIPAA 164.312(a)(2)(iii) (automatic logoff) and 164.312(d). A sign-in creates a session
*family*; every refresh rotates the token (the old one is marked ``rotated``). The
session ends when:

* it has not been refreshed for ``session_idle_minutes`` (server-enforced idle logoff),
* it is older than ``session_absolute_hours``,
* the user's ``token_version`` changed (password change, admin revoke, invitation reset),
* the account is inactive / no longer loadable,
* it is signed out, or
* an already-rotated token is presented again — refresh-token reuse means the token was
  copied, so the whole family is revoked and the event audited.

Only the SHA-256 of a token is stored. All lookups run under a platform DB scope because
refresh happens before any tenant is known (the caller binds it; see AUTH_PATHS).
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update

from app.config import get_settings
from app.domain.models import AuditEntry

logger = structlog.get_logger(__name__)

# A rotated token presented again within this window is treated as a benign race.
_REUSE_GRACE_SECONDS = 10


class SessionRejected(Exception):
    """The refresh token cannot continue a session; ``reason`` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RefreshedSession:
    refresh_token: str
    user_id: str
    tenant_id: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class RefreshSessionService:
    def __init__(self, session):
        self._session = session

    async def start(
        self, user_id: str, tenant_id: str, token_version: int,
        client_ip: str = "", user_agent: str = "",
    ) -> str:
        """New session family at sign-in; returns the raw refresh token."""
        settings = get_settings()
        return await self._insert(
            user_id, tenant_id, token_version, family_id=str(uuid.uuid4()),
            expires_at=_now() + timedelta(hours=settings.session_absolute_hours),
            client_ip=client_ip, user_agent=user_agent,
        )

    async def rotate(self, raw_token: str, client_ip: str = "", user_agent: str = "") -> RefreshedSession:
        """Exchange a refresh token for a new one (same family, same absolute expiry)."""
        from app.infrastructure.auth.principal import load_principal
        from app.infrastructure.database.models import RefreshSessionRecord

        settings = get_settings()
        record = (
            await self._session.execute(
                select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == _hash(raw_token))
            )
        ).scalar_one_or_none()
        if record is None:
            raise SessionRejected("unknown")
        now = _now()
        if record.revoked_at is not None:
            rotated_at = _aware(record.rotated_at)
            if (
                record.revoke_reason == "rotated"
                and rotated_at is not None
                and now - rotated_at <= timedelta(seconds=_REUSE_GRACE_SECONDS)
            ):
                # Two requests (tabs, a network retry) raced with the same token: the
                # winner already holds the rotated one. Refuse this one, don't punish.
                raise SessionRejected("rotated_recently")
            if record.revoke_reason == "rotated":
                # A token that was already exchanged is being replayed: assume theft.
                await self.revoke_family(record.family_id, "reuse_detected")
                await self._audit("session_reuse_detected", record, client_ip)
            raise SessionRejected(record.revoke_reason or "revoked")
        if _aware(record.expires_at) <= now:
            await self._revoke(record.id, "expired")
            raise SessionRejected("expired")
        if _aware(record.last_used_at) + timedelta(minutes=settings.session_idle_minutes) <= now:
            await self._revoke(record.id, "idle")
            raise SessionRejected("idle")
        principal = await load_principal(record.user_id, record.tenant_id)
        if principal is None:
            await self.revoke_family(record.family_id, "inactive")
            raise SessionRejected("inactive")
        if principal.token_version != record.token_version:
            await self.revoke_family(record.family_id, "token_version")
            raise SessionRejected("token_version")

        await self._session.execute(
            update(RefreshSessionRecord)
            .where(RefreshSessionRecord.id == record.id)
            .values(rotated_at=now, revoked_at=now, revoke_reason="rotated", last_used_at=now)
        )
        new_token = await self._insert(
            record.user_id, record.tenant_id, record.token_version,
            family_id=record.family_id, expires_at=_aware(record.expires_at),
            client_ip=client_ip, user_agent=user_agent,
        )
        return RefreshedSession(new_token, record.user_id, record.tenant_id)

    async def end(self, raw_token: str, reason: str = "logout") -> None:
        """Sign-out: revoke the presented token's whole family."""
        from app.infrastructure.database.models import RefreshSessionRecord

        record = (
            await self._session.execute(
                select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == _hash(raw_token))
            )
        ).scalar_one_or_none()
        if record is not None:
            await self.revoke_family(record.family_id, reason)

    async def revoke_family(self, family_id: str, reason: str) -> None:
        from app.infrastructure.database.models import RefreshSessionRecord

        await self._session.execute(
            update(RefreshSessionRecord)
            .where(RefreshSessionRecord.family_id == family_id, RefreshSessionRecord.revoked_at.is_(None))
            .values(revoked_at=_now(), revoke_reason=reason)
        )
        await self._session.flush()

    async def _revoke(self, record_id: str, reason: str) -> None:
        from app.infrastructure.database.models import RefreshSessionRecord

        await self._session.execute(
            update(RefreshSessionRecord)
            .where(RefreshSessionRecord.id == record_id)
            .values(revoked_at=_now(), revoke_reason=reason)
        )
        await self._session.flush()

    async def _insert(
        self, user_id: str, tenant_id: str, token_version: int, *, family_id: str,
        expires_at: datetime, client_ip: str, user_agent: str,
    ) -> str:
        from app.infrastructure.database.models import RefreshSessionRecord

        raw = secrets.token_urlsafe(48)
        self._session.add(RefreshSessionRecord(
            id=str(uuid.uuid4()), tenant_id=tenant_id, user_id=user_id, family_id=family_id,
            token_hash=_hash(raw), token_version=token_version or 0, expires_at=expires_at,
            last_used_at=_now(), client_ip=(client_ip or "")[:64] or None,
            user_agent=(user_agent or "")[:256] or None,
        ))
        await self._session.flush()
        return raw

    async def _audit(self, action: str, record, client_ip: str) -> None:
        from app.infrastructure.database.repositories import PgAuditRepository

        await PgAuditRepository(self._session, tenant_id=record.tenant_id).save(AuditEntry(
            action=action, entity_type="user", entity_id=record.user_id, actor=record.user_id,
            details={"family_id": record.family_id, "client_ip": client_ip},
            tenant_id=record.tenant_id,
        ))
        logger.warning("refresh_token_reuse_detected", user_id=record.user_id, family=record.family_id)
