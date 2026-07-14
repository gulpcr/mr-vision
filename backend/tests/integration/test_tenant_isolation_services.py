"""Tenant isolation for the application-service layer — against a real schema
(SQLite in-memory), not mocks. Mirrors test_tenant_isolation.py's pattern but
covers the services fixed in the tenant-isolation audit: critical alerts,
review queue, share links, retention policies, batch uploads, and tenant API
keys. Each of these was found NOT to filter by tenant_id before that audit.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.active_learning_service import ActiveLearningService
from app.application.alerting_service import AlertingService
from app.application.batch_service import BatchUploadService
from app.application.portal_service import PortalService
from app.application.retention_service import RetentionService
from app.application.tenant_api_key_service import TenantApiKeyService
from app.infrastructure.database.models import Base, StudyRecord
from app.infrastructure.database.repositories import PgTenantApiKeyRepository


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


async def _seed_study(session, study_uid: str, tenant_id: str) -> None:
    session.add(StudyRecord(study_instance_uid=study_uid, modality="MR", tenant_id=tenant_id))
    await session.flush()


class TestAlertingServiceIsolation:
    @pytest.mark.asyncio
    async def test_list_critical_alerts_scoped_to_tenant(self, db_session):
        svc = AlertingService(db_session)
        await svc.evaluate_result_alerts(
            study_instance_uid=_uid(), usecase_name="brain_mri", result_id=str(uuid.uuid4()),
            measurements={"whole_tumor_volume_ml": 150}, summary={}, qa_flags=[],
            tenant_id="hospital-a",
        )
        await svc.evaluate_result_alerts(
            study_instance_uid=_uid(), usecase_name="brain_mri", result_id=str(uuid.uuid4()),
            measurements={"whole_tumor_volume_ml": 150}, summary={}, qa_flags=[],
            tenant_id="hospital-b",
        )
        await db_session.commit()

        alerts_a = await svc.list_critical_alerts(tenant_id="hospital-a")
        alerts_b = await svc.list_critical_alerts(tenant_id="hospital-b")
        assert len(alerts_a) == 1
        assert len(alerts_b) == 1
        assert alerts_a[0]["id"] != alerts_b[0]["id"]

    @pytest.mark.asyncio
    async def test_get_critical_alert_returns_none_for_other_tenant(self, db_session):
        svc = AlertingService(db_session)
        await svc.evaluate_result_alerts(
            study_instance_uid=_uid(), usecase_name="brain_mri", result_id=str(uuid.uuid4()),
            measurements={"whole_tumor_volume_ml": 150}, summary={}, qa_flags=[],
            tenant_id="hospital-a",
        )
        await db_session.commit()
        alert_id = (await svc.list_critical_alerts(tenant_id="hospital-a"))[0]["id"]

        assert await svc.get_critical_alert(alert_id, tenant_id="hospital-b") is None
        assert await svc.get_critical_alert(alert_id, tenant_id="hospital-a") is not None

    @pytest.mark.asyncio
    async def test_acknowledge_critical_alert_blocked_across_tenant(self, db_session):
        svc = AlertingService(db_session)
        await svc.evaluate_result_alerts(
            study_instance_uid=_uid(), usecase_name="brain_mri", result_id=str(uuid.uuid4()),
            measurements={"whole_tumor_volume_ml": 150}, summary={}, qa_flags=[],
            tenant_id="hospital-a",
        )
        await db_session.commit()
        alert_id = (await svc.list_critical_alerts(tenant_id="hospital-a"))[0]["id"]

        assert await svc.acknowledge_critical_alert(alert_id, "dr-b", tenant_id="hospital-b") is None
        acked = await svc.acknowledge_critical_alert(alert_id, "dr-a", tenant_id="hospital-a")
        assert acked is not None and acked["status"] == "acknowledged"

    @pytest.mark.asyncio
    async def test_get_critical_alert_stats_scoped_to_tenant(self, db_session):
        svc = AlertingService(db_session)
        await svc.evaluate_result_alerts(
            study_instance_uid=_uid(), usecase_name="brain_mri", result_id=str(uuid.uuid4()),
            measurements={"whole_tumor_volume_ml": 150}, summary={}, qa_flags=[],
            tenant_id="hospital-a",
        )
        await db_session.commit()

        assert (await svc.get_critical_alert_stats(tenant_id="hospital-a"))["pending_critical"] == 1
        assert (await svc.get_critical_alert_stats(tenant_id="hospital-b"))["pending_critical"] == 0

    @pytest.mark.asyncio
    async def test_alert_rule_trigger_scoped_to_tenant(self, db_session):
        """A webhook rule created for tenant A must not fire for tenant B's events.

        Asserts via alert history (a row is written whether or not the webhook
        delivery itself succeeds) rather than trigger_alert's return count,
        since that count only reflects actual network delivery — not
        something this test controls or cares about.
        """
        svc = AlertingService(db_session)
        await svc.create_rule(
            name="rule-a", event_type="result_ready", webhook_url="http://example.invalid/hook",
            tenant_id="hospital-a",
        )
        await db_session.commit()

        await svc.trigger_alert("result_ready", {"foo": "bar"}, tenant_id="hospital-b")
        await db_session.commit()
        assert len(await svc.get_history(tenant_id="hospital-b")) == 0

        await svc.trigger_alert("result_ready", {"foo": "bar"}, tenant_id="hospital-a")
        await db_session.commit()
        assert len(await svc.get_history(tenant_id="hospital-a")) == 1

    @pytest.mark.asyncio
    async def test_delete_rule_blocked_across_tenant(self, db_session):
        svc = AlertingService(db_session)
        rule = await svc.create_rule(
            name="rule-a", event_type="result_ready", webhook_url="http://example.invalid/hook",
            tenant_id="hospital-a",
        )
        await db_session.commit()

        assert await svc.delete_rule(rule["id"], tenant_id="hospital-b") is False
        assert await svc.delete_rule(rule["id"], tenant_id="hospital-a") is True


class TestActiveLearningServiceIsolation:
    @pytest.mark.asyncio
    async def test_list_review_queue_scoped_to_tenant(self, db_session):
        svc = ActiveLearningService(db_session)
        await svc.add_to_review_queue(_uid(), "brain_mri", str(uuid.uuid4()), 0.4, tenant_id="hospital-a")
        await svc.add_to_review_queue(_uid(), "brain_mri", str(uuid.uuid4()), 0.4, tenant_id="hospital-b")
        await db_session.commit()

        assert len(await svc.list_review_queue(tenant_id="hospital-a")) == 1
        assert len(await svc.list_review_queue(tenant_id="hospital-b")) == 1

    @pytest.mark.asyncio
    async def test_get_review_item_returns_none_for_other_tenant(self, db_session):
        svc = ActiveLearningService(db_session)
        await svc.add_to_review_queue(_uid(), "brain_mri", str(uuid.uuid4()), 0.4, tenant_id="hospital-a")
        await db_session.commit()
        review_id = (await svc.list_review_queue(tenant_id="hospital-a"))[0]["id"]

        assert await svc.get_review_item(review_id, tenant_id="hospital-b") is None
        assert await svc.get_review_item(review_id, tenant_id="hospital-a") is not None

    @pytest.mark.asyncio
    async def test_submit_review_blocked_across_tenant(self, db_session):
        svc = ActiveLearningService(db_session)
        await svc.add_to_review_queue(_uid(), "brain_mri", str(uuid.uuid4()), 0.4, tenant_id="hospital-a")
        await db_session.commit()
        review_id = (await svc.list_review_queue(tenant_id="hospital-a"))[0]["id"]

        assert await svc.submit_review(review_id, "approved", "dr-b", tenant_id="hospital-b") is None
        result = await svc.submit_review(review_id, "approved", "dr-a", tenant_id="hospital-a")
        assert result is not None and result["status"] == "approved"


class TestPortalServiceIsolation:
    @pytest.mark.asyncio
    async def test_share_link_tagged_with_creating_tenant(self, db_session):
        svc = PortalService(db_session)
        result_id = str(uuid.uuid4())
        await svc.create_share_link(result_id, _uid(), "brain_mri", tenant_id="hospital-a")
        await db_session.commit()

        links_a = await svc.list_links_for_result(result_id, tenant_id="hospital-a")
        links_b = await svc.list_links_for_result(result_id, tenant_id="hospital-b")
        assert len(links_a) == 1
        assert len(links_b) == 0

    @pytest.mark.asyncio
    async def test_revoke_link_blocked_across_tenant(self, db_session):
        svc = PortalService(db_session)
        result_id = str(uuid.uuid4())
        created = await svc.create_share_link(result_id, _uid(), "brain_mri", tenant_id="hospital-a")
        await db_session.commit()

        assert await svc.revoke_link(created["id"], tenant_id="hospital-b") is False
        assert await svc.revoke_link(created["id"], tenant_id="hospital-a") is True


class TestRetentionServiceIsolation:
    @pytest.mark.asyncio
    async def test_apply_policies_only_purges_owning_tenants_studies(self, db_session):
        """Regression test for the cross-tenant purge bug: a retention policy
        scoped to hospital-a must never delete hospital-b's old records, even
        though both rows are equally old and match the same entity_type.
        """
        from datetime import datetime, timedelta

        old_uid_a, old_uid_b = _uid(), _uid()
        ancient = datetime.utcnow() - timedelta(days=400)
        db_session.add(StudyRecord(
            study_instance_uid=old_uid_a, modality="MR", tenant_id="hospital-a", created_at=ancient,
        ))
        db_session.add(StudyRecord(
            study_instance_uid=old_uid_b, modality="MR", tenant_id="hospital-b", created_at=ancient,
        ))
        await db_session.commit()

        svc = RetentionService(db_session)
        await svc.create_policy(
            name="purge-old-studies", entity_type="study", max_age_days=30, action="delete",
            tenant_id="hospital-a",
        )
        await db_session.commit()

        await svc.apply_policies(tenant_id="hospital-a")
        await db_session.commit()

        from sqlalchemy import select
        remaining = (await db_session.execute(select(StudyRecord.study_instance_uid))).scalars().all()
        assert old_uid_a not in remaining
        assert old_uid_b in remaining

    @pytest.mark.asyncio
    async def test_delete_policy_blocked_across_tenant(self, db_session):
        svc = RetentionService(db_session)
        policy = await svc.create_policy(
            name="policy-a", entity_type="study", tenant_id="hospital-a",
        )
        await db_session.commit()

        assert await svc.delete_policy(policy["id"], tenant_id="hospital-b") is False
        assert await svc.delete_policy(policy["id"], tenant_id="hospital-a") is True


class TestBatchUploadServiceIsolation:
    @pytest.mark.asyncio
    async def test_get_batch_returns_none_for_other_tenant(self, db_session):
        svc = BatchUploadService(db_session)
        created = await svc.create_batch("batch-a", [_uid(), _uid()], tenant_id="hospital-a")
        await db_session.commit()

        assert await svc.get_batch(created["id"], tenant_id="hospital-b") is None
        assert await svc.get_batch(created["id"], tenant_id="hospital-a") is not None

    @pytest.mark.asyncio
    async def test_list_batches_scoped_to_tenant(self, db_session):
        svc = BatchUploadService(db_session)
        await svc.create_batch("batch-a", [_uid()], tenant_id="hospital-a")
        await svc.create_batch("batch-b", [_uid()], tenant_id="hospital-b")
        await db_session.commit()

        assert len(await svc.list_batches(tenant_id="hospital-a")) == 1
        assert len(await svc.list_batches(tenant_id="hospital-b")) == 1


class TestTenantApiKeyServiceIsolation:
    @pytest.mark.asyncio
    async def test_list_keys_scoped_to_tenant(self, db_session):
        svc = TenantApiKeyService(PgTenantApiKeyRepository(db_session))
        await svc.create_key(tenant_id="hospital-a", name="key-a", scopes=["dicom:upload"])
        await svc.create_key(tenant_id="hospital-b", name="key-b", scopes=["dicom:upload"])
        await db_session.commit()

        assert len(await svc.list_keys("hospital-a")) == 1
        assert len(await svc.list_keys("hospital-b")) == 1

    @pytest.mark.asyncio
    async def test_revoke_key_rejected_for_wrong_tenant(self, db_session):
        svc = TenantApiKeyService(PgTenantApiKeyRepository(db_session))
        key, _plain = await svc.create_key(tenant_id="hospital-a", name="key-a", scopes=["dicom:upload"])
        await db_session.commit()

        with pytest.raises(ValueError, match="not found for this tenant"):
            await svc.revoke_key(tenant_id="hospital-b", key_id=key.id)

        revoked = await svc.revoke_key(tenant_id="hospital-a", key_id=key.id)
        assert revoked.is_active is False

    @pytest.mark.asyncio
    async def test_validate_key_resolves_the_correct_tenant(self, db_session):
        """The DICOM upload gateway trusts whatever tenant validate_key resolves —
        a key minted for hospital-a must never resolve to hospital-b."""
        svc = TenantApiKeyService(PgTenantApiKeyRepository(db_session))
        _key, plain = await svc.create_key(tenant_id="hospital-a", name="key-a", scopes=["dicom:upload"])
        await db_session.commit()

        resolved = await svc.validate_key(plain)
        assert resolved.tenant_id == "hospital-a"

        with pytest.raises(ValueError, match="Invalid or revoked"):
            await svc.validate_key("mrv_" + "0" * 64)
