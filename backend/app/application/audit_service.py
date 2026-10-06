"""AuditService — writes AuditEvent-shaped entries, including PHI *read* access.

Two problems this addresses.

**Untyped actors.** ``audit_log.actor`` was one String(128) holding four different kinds of
value depending on the call site — "system", "celery_worker", a user id, or a username.
Given a row you could not tell which, so it could not be resolved to a Practitioner and
could not be joined to ``users`` without guessing. Entries written here always carry a
typed ``actor_type`` and, for people, the stable ``actor_id`` (never a username, which
changes).

**Unlogged reads.** ``AuditAction.RESULT_VIEWED`` has existed in the enum since the
beginning but nothing ever wrote it: viewing a result, downloading a report PDF, and
redeeming a share-link token all left no trace. That is the most consequential gap in the
audit pillar and it is a compliance matter independent of FHIR — an access review asks
"who looked at this patient's record", and the data to answer it was not being recorded.
``record_read`` exists to make those writes one line at the call site.

Failures are logged and swallowed: an audit write must never fail the request it describes.
The trade-off is deliberate and worth naming — a dropped audit entry is bad, but a clinician
blocked from opening a study is worse, and the structured log still carries the event.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditActorType, audit_action_to_crude
from app.infrastructure.database.models import AuditLogRecord

logger = structlog.get_logger(__name__)

# AuditEvent.outcome codes (http://hl7.org/fhir/ValueSet/audit-event-outcome).
OUTCOME_SUCCESS = "0"
OUTCOME_MINOR_FAILURE = "4"
OUTCOME_SERIOUS_FAILURE = "8"


def actor_from_request(request: Any) -> tuple[str | None, str | None, str]:
    """Extract ``(actor_id, actor_display, client_ip)`` from a FastAPI request.

    Reads what the auth middleware already put on ``request.state`` — no extra queries.
    Returns ``(None, None, ip)`` for an unauthenticated call rather than inventing an actor.
    """
    if request is None:
        return None, None, ""
    state = getattr(request, "state", None)
    # The auth middleware sets `user` (username) and `user_id` (JWT sub). user_id is empty
    # for api_key/none auth modes, in which case there is no person to attribute to and
    # actor_type falls through to "system" rather than inventing one.
    actor_id = getattr(state, "user_id", None) or None
    display = getattr(state, "user", None) or None
    return actor_id, display, client_ip_from(request)


def client_ip_from(request: Any) -> str:
    """The caller's IP. Behind nginx the socket peer is nginx itself, so prefer the
    X-Real-IP header nginx sets from $remote_addr (clients cannot inject it: nginx
    overwrites it, and the backend port is bound to localhost)."""
    if request is None:
        return ""
    headers = getattr(request, "headers", None)
    real_ip = headers.get("x-real-ip", "") if headers is not None else ""
    if real_ip:
        return real_ip.strip()
    client = getattr(request, "client", None)
    return (getattr(client, "host", "") or "") if client else ""


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        action: str,
        entity_type: str,
        entity_id: str,
        *,
        actor_id: str | None = None,
        actor_display: str | None = None,
        actor_type: AuditActorType | None = None,
        client_ip: str | None = None,
        outcome: str = OUTCOME_SUCCESS,
        source_observer: str = "backend",
        details: dict[str, Any] | None = None,
        commit: bool = False,
        before: Any = None,
        after: Any = None,
    ) -> bool:
        """Write one audit entry. Returns True if it was persisted.

        ``actor_type`` is inferred when omitted: an ``actor_id`` means a person, its
        absence means the platform acted on its own. A caller representing an algorithm
        passes ``AuditActorType.DEVICE`` explicitly.

        ``before`` / ``after``: the changed record's previous and new state; only their
        SHA-256 is stored (domain/audit_state.py).
        """
        try:
            from app.infrastructure.database.repositories import next_audit_chain_link
            from app.infrastructure.tenant.db_scope import current_scope_tenant_id

            resolved_type = actor_type or (
                AuditActorType.PRACTITIONER if actor_id else AuditActorType.SYSTEM
            )
            legacy_actor = actor_id or actor_display or "system"
            entity = str(entity_id)[:256]
            # Outcome and client IP go into the hashed payload too, so they are
            # tamper-evident like the rest of the row.
            payload = dict(details or {})
            if before is not None or after is not None:
                from app.domain.audit_state import state_change_details

                payload.update(state_change_details(before, after))
            if client_ip:
                payload.setdefault("client_ip", client_ip)
            if outcome != OUTCOME_SUCCESS:
                payload.setdefault("outcome", outcome)
            tenant_id = current_scope_tenant_id() or "default"
            seq, prev_hash, row_hash = await next_audit_chain_link(
                self._session, action=action, entity_type=entity_type, entity_id=entity,
                actor=legacy_actor, details=payload, tenant_id=tenant_id,
            )
            record = AuditLogRecord(
                action=action,
                entity_type=entity_type,
                entity_id=entity,
                # Legacy column still written so /api/audit's existing actor filter and
                # any current reader keep working.
                actor=legacy_actor,
                actor_type=resolved_type.value,
                actor_id=actor_id,
                actor_display=actor_display,
                action_crude=audit_action_to_crude(action),
                outcome=outcome,
                source_observer=source_observer,
                client_ip=(client_ip or None),
                details=payload,
                tenant_id=tenant_id,
                seq=seq,
                prev_hash=prev_hash,
                row_hash=row_hash,
            )
            self._session.add(record)
            await self._session.flush()
            if commit:
                await self._session.commit()
            return True
        except Exception as exc:
            # Never fail the described request because its audit entry could not be
            # written; the structured log preserves the event either way.
            logger.warning(
                "audit_write_failed", action=action, entity_id=str(entity_id)[:64],
                error=str(exc),
            )
            return False

    async def record_read(
        self,
        request: Any,
        action: str,
        entity_type: str,
        entity_id: str,
        details: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        """Log a PHI *read* (AuditEvent.action = R) from a request context.

        Defaults to committing: a read is usually the only thing the request does, so
        without a commit the entry would be discarded when the session closes.
        """
        actor_id, display, ip = actor_from_request(request)
        return await self.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
            actor_display=display,
            client_ip=ip,
            details=details,
            commit=commit,
        )
