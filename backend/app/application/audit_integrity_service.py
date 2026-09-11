"""Verifies the audit_log hash chain hasn't been tampered with.

See app.domain.audit_chain and PgAuditRepository.save() for how the chain is
written. Walking it back and recomputing catches: content edits (row_hash
mismatch), deleted rows (a gap in seq), and reordered/spliced rows (prev_hash
no longer matches the predecessor's row_hash).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit_chain import compute_audit_row_hash


class AuditIntegrityService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def verify_chain(self) -> dict[str, Any]:
        from app.config import derive_secret
        from app.infrastructure.database.models import AuditLogRecord

        secret = derive_secret("audit-chain-v1")

        stmt = (
            select(AuditLogRecord)
            .where(AuditLogRecord.seq.isnot(None))
            .order_by(AuditLogRecord.seq.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()

        expected_seq = 1
        expected_prev_hash: str | None = None
        for row in rows:
            if row.seq != expected_seq:
                return self._broken(expected_seq - 1, row.seq, "sequence gap — a row was deleted")
            if row.prev_hash != expected_prev_hash:
                return self._broken(
                    expected_seq - 1, row.seq,
                    "prev_hash does not match the previous row's row_hash — rows were reordered or spliced",
                )
            recomputed = compute_audit_row_hash(
                secret=secret,
                seq=row.seq,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                actor=row.actor,
                details=row.details,
                tenant_id=row.tenant_id,
                prev_hash=row.prev_hash,
            )
            if recomputed != row.row_hash:
                return self._broken(
                    expected_seq - 1, row.seq,
                    "row_hash does not match recomputed hash — row contents were altered",
                )
            expected_prev_hash = row.row_hash
            expected_seq += 1

        return {"valid": True, "checked": len(rows), "broken_at_seq": None, "reason": None}

    @staticmethod
    def _broken(checked: int, broken_at_seq: int, reason: str) -> dict[str, Any]:
        return {"valid": False, "checked": checked, "broken_at_seq": broken_at_seq, "reason": reason}
