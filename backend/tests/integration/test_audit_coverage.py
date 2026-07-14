"""Broadened audit coverage — spot-checks that previously-unused AuditAction
values are now actually written at their real call sites, and that the
Celery (sync-session) write path participates in the same hash chain as the
async path instead of silently falling outside it.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.alerting_service import AlertingService
from app.application.audit_integrity_service import AuditIntegrityService
from app.application.auth_service import AuthService
from app.application.retention_service import RetentionService
from app.infrastructure.database.models import AuditLogRecord, Base, StudyRecord


def _uid() -> str:
    return f"1.2.{uuid.uuid4().int % 10**12}"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


class TestLoginAndRegistrationAudit:
    @pytest.mark.asyncio
    async def test_create_user_writes_user_created(self, db_session):
        svc = AuthService(db_session)
        user = await svc.create_user(username="alice", email="alice@example.com", password="pw12345678")
        await db_session.commit()

        rows = (await db_session.execute(
            select(AuditLogRecord).where(AuditLogRecord.action == "user_created")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == user.id

    @pytest.mark.asyncio
    async def test_successful_login_writes_user_login(self, db_session):
        svc = AuthService(db_session)
        await svc.create_user(username="bob", email="bob@example.com", password="pw12345678")
        await db_session.commit()

        result = await svc.authenticate("bob", "pw12345678")
        assert result["mfa_required"] is False
        await db_session.commit()

        rows = (await db_session.execute(
            select(AuditLogRecord).where(AuditLogRecord.action == "user_login")
        )).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_failed_login_writes_no_audit_entry(self, db_session):
        svc = AuthService(db_session)
        await svc.create_user(username="carol", email="carol@example.com", password="pw12345678")
        await db_session.commit()

        result = await svc.authenticate("carol", "wrong-password")
        assert result is None

        rows = (await db_session.execute(
            select(AuditLogRecord).where(AuditLogRecord.action == "user_login")
        )).scalars().all()
        assert len(rows) == 0


class TestAlertTriggeredAudit:
    @pytest.mark.asyncio
    async def test_successful_webhook_writes_alert_triggered(self, db_session, monkeypatch):
        svc = AlertingService(db_session)
        rule = await svc.create_rule(
            name="rule-a", event_type="result_ready", webhook_url="http://example.invalid/hook",
            tenant_id="hospital-a",
        )
        await db_session.commit()

        monkeypatch.setattr(AlertingService, "_send_webhook", staticmethod(lambda *a, **k: _true()))
        await svc.trigger_alert("result_ready", {"foo": "bar"}, tenant_id="hospital-a")
        await db_session.commit()

        rows = (await db_session.execute(
            select(AuditLogRecord).where(AuditLogRecord.action == "alert_triggered")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == rule["id"]


async def _true():
    return True


class TestRetentionPurgeAudit:
    @pytest.mark.asyncio
    async def test_purge_writes_data_purged_entry(self, db_session):
        from datetime import datetime, timedelta

        old_uid = _uid()
        ancient = datetime.utcnow() - timedelta(days=400)
        db_session.add(StudyRecord(
            study_instance_uid=old_uid, modality="MR", tenant_id="hospital-a", created_at=ancient,
        ))
        await db_session.commit()

        svc = RetentionService(db_session)
        await svc.create_policy(
            name="purge-old", entity_type="study", max_age_days=30, action="delete",
            tenant_id="hospital-a",
        )
        await db_session.commit()

        await svc.apply_policies(tenant_id="hospital-a")
        await db_session.commit()

        rows = (await db_session.execute(
            select(AuditLogRecord).where(AuditLogRecord.action == "data_purged")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].details["count"] == 1


class TestSyncWriteAuditChainParticipation:
    """See tests/unit/infrastructure/test_write_audit_chain.py for the direct
    _write_audit test — tests/integration/conftest.py mocks out the whole
    app.infrastructure.queue.tasks module (so the FastAPI app can import
    without Celery/heavy ML deps), which would make _write_audit a no-op
    MagicMock here instead of the real function.
    """

    @pytest.mark.asyncio
    async def test_mixed_async_and_sync_writes_form_one_unbroken_chain(self, db_session):
        """A more realistic scenario: an async (FastAPI) write followed by a
        sync (Celery) write against the SAME table must chain together, not
        just internally within each write path.
        """
        from app.domain.enums import AuditAction
        from app.domain.models import AuditEntry
        from app.infrastructure.database.repositories import PgAuditRepository

        await PgAuditRepository(db_session).save(AuditEntry(
            action=AuditAction.STUDY_RECEIVED, entity_type="study", entity_id="study-1",
            actor="alice", tenant_id="hospital-a",
        ))
        await db_session.commit()

        # Simulate the sync Celery write hitting the SAME underlying SQLite
        # file — use a raw sync engine against the same in-memory DB via a
        # shared connection isn't possible for :memory:, so instead verify
        # the chain-extension logic directly against this async session
        # using the same primitive the sync path calls.
        from app.domain.audit_chain import compute_audit_row_hash
        from app.config import derive_secret

        tail = (await db_session.execute(
            select(AuditLogRecord.seq, AuditLogRecord.row_hash)
            .where(AuditLogRecord.seq.isnot(None))
            .order_by(AuditLogRecord.seq.desc())
            .limit(1)
        )).first()
        assert tail is not None
        next_seq = tail.seq + 1
        secret = derive_secret("audit-chain-v1")
        row_hash = compute_audit_row_hash(
            secret=secret, seq=next_seq, action="job_completed", entity_type="job",
            entity_id="job-1", actor="celery_worker", details={}, tenant_id="hospital-a",
            prev_hash=tail.row_hash,
        )
        db_session.add(AuditLogRecord(
            id=str(uuid.uuid4()), action="job_completed", entity_type="job", entity_id="job-1",
            actor="celery_worker", details={}, tenant_id="hospital-a",
            seq=next_seq, prev_hash=tail.row_hash, row_hash=row_hash,
        ))
        await db_session.commit()

        result = await AuditIntegrityService(db_session).verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 2
