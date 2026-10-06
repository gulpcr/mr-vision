"""W2-13 audit operations: security alert rules (ADM-04 / PRV-05), the monthly signed
audit-log archive (PRV-06) and its WORM (Object Lock, compliance mode) storage.

Same harness as test_audit_review.py. The WORM part needs the TLS test stack's MinIO with
the audit-archive bucket created by minio/init.sh (RLS_TEST_WITH_ORTHANC=1 stack).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import uuid

import pytest

from tests.rls.test_audit_review import _write_chained
from tests.rls.test_tenant_isolation import OWNER_URL

pytestmark = pytest.mark.skipif(not OWNER_URL, reason="RLS_TEST_OWNER_DATABASE_URL not set")

TS = f"rls_sa_{uuid.uuid4().hex[:8]}"
MONTH = (2001, 3)  # a month no other test writes into


def _run(coro_factory):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.infrastructure.tls import db_async_connect_args

    async def go():
        url = OWNER_URL.split("?", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
        eng = create_async_engine(url, connect_args=db_async_connect_args())
        try:
            async with AsyncSession(eng) as s:
                out = await coro_factory(s)
                await s.commit()
                return out
        finally:
            await eng.dispose()

    return asyncio.new_event_loop().run_until_complete(go())


@pytest.fixture(scope="module")
def alert_events():
    base = "2001-03-10T12:0"
    events = [("account_locked", f"{base}0:00+00:00", TS, {"reason": "x"}),
              ("break_glass_invoked", f"{base}1:00+00:00", TS, {"reason": "ed"}),
              ("signed_document_integrity_failed", f"{base}2:00+00:00", TS, {}),
              ("session_reuse_detected", f"{base}3:00+00:00", TS, {})]
    events += [("login_failed", f"{base}4:{i:02d}+00:00", TS, {}) for i in range(12)]
    events += [("phi_accessed", f"{base}5:00+00:00", TS, {})]  # not an alert
    _write_chained(events)
    return events


def test_security_alert_rules(alert_events):
    from datetime import datetime

    from app.application.security_alert_service import SecurityAlertService

    start = datetime.fromisoformat("2001-03-10T11:55:00+00:00")
    end = datetime.fromisoformat("2001-03-10T12:06:00+00:00")
    alerts = [a for a in _run(lambda s: SecurityAlertService(s, 10).evaluate(start, end))
              if a.tenant_id == TS]
    kinds = sorted(a.kind for a in alerts)
    assert kinds == ["account_locked", "emergency_access", "failed_sign_in_burst",
                     "integrity", "session_theft"]
    burst = next(a for a in alerts if a.kind == "failed_sign_in_burst")
    assert burst.count == 12 and burst.details["client_ip"] == "203.0.113.9"
    assert next(a for a in alerts if a.kind == "integrity").severity == "critical"
    # De-dup keys are stable across overlapping windows.
    again = [a for a in _run(lambda s: SecurityAlertService(s, 10).evaluate(start, end))
             if a.tenant_id == TS]
    assert sorted(a.key for a in again) == sorted(a.key for a in alerts)
    # Below the burst threshold: no burst alert.
    quiet = _run(lambda s: SecurityAlertService(s, 50).evaluate(start, end))
    assert not any(a.kind == "failed_sign_in_burst" and a.tenant_id == TS for a in quiet)


class _MemStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put_locked(self, key, data, content_type):
        from datetime import datetime, timezone

        assert key not in self.objects, "archive objects are never overwritten"
        self.objects[key] = data
        return datetime(2030, 1, 1, tzinfo=timezone.utc)

    async def exists(self, key):
        return key in self.objects


def test_monthly_archive_manifest_and_idempotency(alert_events):
    from app.application.audit_archive_service import (
        AuditArchiveService,
        archive_hmac,
        month_bounds,
    )

    store = _MemStore()
    start, end = month_bounds(*MONTH)
    out = _run(lambda s: AuditArchiveService(s, store).export_month(start, end))
    assert out["status"] == "exported" and out["prefix"] == "audit-log/2001/2001-03"
    log = store.objects["audit-log/2001/2001-03/audit_log.jsonl.gz"]
    manifest = json.loads(store.objects["audit-log/2001/2001-03/manifest.json"])
    rows = [json.loads(line) for line in gzip.decompress(log).decode().splitlines()]
    mine = [r for r in rows if r["tenant_id"] == TS]
    assert len(mine) == len(alert_events)
    assert all(r["row_hash"] and r["seq"] for r in mine)
    assert all(r["timestamp"].startswith("2001-03") for r in rows)
    assert manifest["rows"] == len(rows)
    assert manifest["files"]["audit_log.jsonl.gz"]["sha256"] == hashlib.sha256(log).hexdigest()
    assert manifest["files"]["audit_log.jsonl.gz"]["hmac_sha256"] == archive_hmac(log)
    assert set(manifest["chain_verification"]) == {"valid", "checked", "broken_at_seq", "reason"}
    # Second run of the same month: nothing rewritten.
    again = _run(lambda s: AuditArchiveService(s, store).export_month(start, end))
    assert again["status"] == "exists"


@pytest.mark.skipif(not os.environ.get("RLS_TEST_WITH_ORTHANC"), reason="needs the TLS test stack")
def test_archive_bucket_is_worm():
    """A locked archive object can be read but not deleted, even by the uploader."""
    from minio.error import S3Error

    from app.infrastructure.storage.audit_archive import AuditArchiveStore

    store = AuditArchiveStore()
    key = f"rls-test/{uuid.uuid4().hex}.json"
    loop = asyncio.new_event_loop()
    until = loop.run_until_complete(store.put_locked(key, b'{"t":1}', "application/json"))
    assert loop.run_until_complete(store.exists(key))
    client, bucket = store._client, store._bucket
    stat = client.stat_object(bucket, key)
    assert stat.version_id, "Object Lock buckets are versioned"
    ret = client.get_object_retention(bucket, key, version_id=stat.version_id)
    assert ret.mode == "COMPLIANCE" and ret.retain_until_date.date() == until.date()
    with pytest.raises(S3Error):
        client.remove_object(bucket, key, version_id=stat.version_id)
    assert loop.run_until_complete(store.exists(key))
