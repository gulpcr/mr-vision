"""Tamper-evident audit hash chain — against a real schema (SQLite in-memory),
not mocks. Confirms PgAuditRepository.save() chains rows correctly and
AuditIntegrityService.verify_chain() detects every category of tampering:
content edits, deleted rows, and reordered/spliced rows.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.audit_integrity_service import AuditIntegrityService
from app.domain.models import AuditEntry
from app.infrastructure.database.models import AuditLogRecord, Base
from app.infrastructure.database.repositories import PgAuditRepository


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_three_entries(session) -> list[AuditLogRecord]:
    repo = PgAuditRepository(session)
    for i in range(3):
        await repo.save(AuditEntry(
            entity_type="job", entity_id=f"job-{i}", actor="alice",
            details={"n": i}, tenant_id="hospital-a",
        ))
    await session.commit()
    result = await session.execute(select(AuditLogRecord).order_by(AuditLogRecord.seq.asc()))
    return list(result.scalars().all())


class TestChainConstruction:
    @pytest.mark.asyncio
    async def test_seq_is_contiguous_starting_at_one(self, db_session):
        rows = await _seed_three_entries(db_session)
        assert [r.seq for r in rows] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_first_row_has_no_prev_hash(self, db_session):
        rows = await _seed_three_entries(db_session)
        assert rows[0].prev_hash is None

    @pytest.mark.asyncio
    async def test_each_row_chains_to_the_previous_rows_hash(self, db_session):
        rows = await _seed_three_entries(db_session)
        assert rows[1].prev_hash == rows[0].row_hash
        assert rows[2].prev_hash == rows[1].row_hash

    @pytest.mark.asyncio
    async def test_row_hash_is_unique_per_row(self, db_session):
        rows = await _seed_three_entries(db_session)
        assert len({r.row_hash for r in rows}) == 3


class TestChainVerification:
    @pytest.mark.asyncio
    async def test_untouched_chain_is_valid(self, db_session):
        await _seed_three_entries(db_session)
        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 3
        assert result["broken_at_seq"] is None

    @pytest.mark.asyncio
    async def test_empty_chain_is_trivially_valid(self, db_session):
        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 0

    @pytest.mark.asyncio
    async def test_detects_content_edit(self, db_session):
        rows = await _seed_three_entries(db_session)
        # Simulate someone editing a row's details directly in the DB,
        # bypassing PgAuditRepository (and thus never recomputing row_hash).
        rows[1].details = {"n": 999, "tampered": True}
        await db_session.commit()

        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is False
        assert result["broken_at_seq"] == 2
        assert "row_hash" in result["reason"]

    @pytest.mark.asyncio
    async def test_detects_deleted_row(self, db_session):
        rows = await _seed_three_entries(db_session)
        await db_session.delete(rows[1])
        await db_session.commit()

        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is False
        assert result["broken_at_seq"] == 3  # seq 2 missing, next row found is seq 3
        assert "gap" in result["reason"]

    @pytest.mark.asyncio
    async def test_detects_reordered_prev_hash(self, db_session):
        rows = await _seed_three_entries(db_session)
        # Splice row 3's prev_hash to point at row 1 instead of row 2 — the
        # kind of tamper that would let someone silently drop row 2's content
        # while keeping seq contiguous.
        rows[2].prev_hash = rows[0].row_hash
        await db_session.commit()

        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is False
        assert result["broken_at_seq"] == 3
        assert "prev_hash" in result["reason"]

    @pytest.mark.asyncio
    async def test_forged_row_hash_without_the_secret_is_still_detected(self, db_session):
        """Recomputing row_hash with a plain hash (no HMAC secret) must not
        pass verification — this is what distinguishes HMAC-chaining from a
        plain SHA-256 chain, which a DB-level attacker could self-consistently
        forge without knowing any secret.
        """
        import hashlib
        import json

        rows = await _seed_three_entries(db_session)
        tampered = rows[1]
        tampered.details = {"n": 999}
        # Attacker recomputes a "plausible" hash using a public hash function,
        # not knowing the HMAC secret used by compute_audit_row_hash.
        canonical = json.dumps(
            {
                "seq": tampered.seq, "action": tampered.action, "entity_type": tampered.entity_type,
                "entity_id": tampered.entity_id, "actor": tampered.actor, "details": tampered.details,
                "tenant_id": tampered.tenant_id, "prev_hash": tampered.prev_hash,
            },
            sort_keys=True, default=str,
        )
        tampered.row_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        await db_session.commit()

        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is False
        assert result["broken_at_seq"] == 2

    @pytest.mark.asyncio
    async def test_legacy_rows_without_seq_are_ignored_by_verification(self, db_session):
        """Pre-migration rows (seq/prev_hash/row_hash all NULL) must not be
        treated as chain breaks — the chain only covers rows written after
        migration 032 introduced it.
        """
        db_session.add(AuditLogRecord(
            action="legacy_action", entity_type="job", entity_id="pre-migration-job",
            actor="system", details={}, tenant_id="default",
        ))
        await db_session.commit()
        await _seed_three_entries(db_session)

        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 3
