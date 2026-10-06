from __future__ import annotations

"""Monthly audit-log archive into WORM storage (PRV-06 6-year retention, TEC-07).

For each closed month: every audit_log row of the month (all tenants — the hash chain is
platform-wide) as JSON Lines, the month's audit reviews, and a manifest with row count,
first/last chain sequence numbers, the chain verification result, the archive's SHA-256
and an HMAC over it (key derived from the platform master key, ``audit-archive-v1``).
Stored with compliance-mode Object Lock; idempotent per month. The database keeps its own
6-year floor (alembic 052); the archive survives even a database loss or a rogue admin.
"""

import gzip
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select

from app.config import derive_secret

logger = structlog.get_logger(__name__)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def previous_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = first - timedelta(days=1)
    return month_bounds(last_month.year, last_month.month)


def archive_hmac(data: bytes) -> str:
    return hmac.new(derive_secret("audit-archive-v1").encode(), data, hashlib.sha256).hexdigest()


class AuditArchiveService:
    def __init__(self, session, store):
        self._session = session
        self._store = store

    async def export_month(self, start: datetime, end: datetime) -> dict[str, Any]:
        from app.application.audit_integrity_service import AuditIntegrityService
        from app.application.audit_service import AuditService
        from app.infrastructure.database.models import AuditLogRecord as A
        from app.infrastructure.database.models import AuditReviewRecord as R

        prefix = f"audit-log/{start:%Y}/{start:%Y-%m}"
        if await self._store.exists(f"{prefix}/manifest.json"):
            return {"status": "exists", "prefix": prefix}

        rows = (await self._session.execute(
            select(A).where(A.timestamp >= start, A.timestamp < end).order_by(A.seq.nullsfirst(), A.timestamp)
        )).scalars().all()
        lines = []
        for r in rows:
            lines.append(json.dumps({
                "id": r.id, "seq": r.seq, "prev_hash": r.prev_hash, "row_hash": r.row_hash,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "tenant_id": r.tenant_id, "action": r.action, "action_crude": r.action_crude,
                "entity_type": r.entity_type, "entity_id": r.entity_id, "actor": r.actor,
                "actor_type": r.actor_type, "actor_id": r.actor_id, "actor_display": r.actor_display,
                "outcome": r.outcome, "client_ip": r.client_ip, "details": r.details,
            }, sort_keys=True, default=str))
        log_bytes = gzip.compress(("\n".join(lines) + ("\n" if lines else "")).encode(), mtime=0)

        reviews = (await self._session.execute(
            select(R).where(R.period_start >= start, R.period_start < end)
        )).scalars().all()
        review_bytes = json.dumps([{
            "id": v.id, "tenant_id": v.tenant_id, "period_start": v.period_start.isoformat(),
            "period_end": v.period_end.isoformat(), "findings_count": v.findings_count,
            "reviewed_by_username": v.reviewed_by_username,
            "reviewed_at": v.reviewed_at.isoformat() if v.reviewed_at else None,
            "review_notes": v.review_notes, "summary": v.summary,
        } for v in reviews], sort_keys=True, default=str).encode()

        chain = await AuditIntegrityService(self._session).verify_chain()
        seqs = [r.seq for r in rows if r.seq is not None]
        manifest = {
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "rows": len(rows), "first_seq": min(seqs) if seqs else None,
            "last_seq": max(seqs) if seqs else None,
            "chain_verification": {k: chain.get(k) for k in ("valid", "checked", "broken_at_seq", "reason")},
            "files": {
                "audit_log.jsonl.gz": {"sha256": hashlib.sha256(log_bytes).hexdigest(),
                                       "hmac_sha256": archive_hmac(log_bytes)},
                "audit_reviews.json": {"sha256": hashlib.sha256(review_bytes).hexdigest(),
                                       "hmac_sha256": archive_hmac(review_bytes)},
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        await self._store.put_locked(f"{prefix}/audit_log.jsonl.gz", log_bytes, "application/gzip")
        await self._store.put_locked(f"{prefix}/audit_reviews.json", review_bytes, "application/json")
        until = await self._store.put_locked(f"{prefix}/manifest.json", manifest_bytes, "application/json")
        await AuditService(self._session).record(
            "audit_archive_exported", "audit_archive", prefix, actor_display="audit_archive_beat",
            details={"rows": len(rows), "retain_until": until.isoformat(),
                     "sha256": manifest["files"]["audit_log.jsonl.gz"]["sha256"],
                     "chain_valid": manifest["chain_verification"]["valid"]},
        )
        return {"status": "exported", "prefix": prefix, **{k: manifest[k] for k in ("rows", "first_seq", "last_seq")},
                "retain_until": until.isoformat()}
