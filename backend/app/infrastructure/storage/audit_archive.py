from __future__ import annotations

"""WORM storage for audit-log archives (checklist PRV-06: immutable 6-year retention).

Objects go to AUDIT_ARCHIVE_BUCKET, created by minio/init.sh with Object Lock in
COMPLIANCE mode and a default retention of AUDIT_ARCHIVE_RETENTION_DAYS (2190 = 6 years):
until that date nobody — not the application, not the MinIO root user — can delete or
overwrite them. Each object is also given an explicit retain-until date, so the guarantee
does not depend on the bucket default alone.
"""

import asyncio
import io
from datetime import datetime, timedelta, timezone

import structlog
from minio import Minio
from minio.commonconfig import COMPLIANCE
from minio.retention import Retention

from app.config import get_settings

logger = structlog.get_logger(__name__)


class AuditArchiveStore:
    def __init__(self):
        from app.infrastructure.tls import minio_http_client

        s = get_settings()
        self._client = Minio(
            s.minio_endpoint, access_key=s.minio_access_key, secret_key=s.minio_secret_key,
            secure=s.minio_secure, http_client=minio_http_client(s),
        )
        self._bucket = s.audit_archive_bucket
        self._days = s.audit_archive_retention_days

    async def put_locked(self, key: str, data: bytes, content_type: str) -> datetime:
        """Upload under compliance-mode retention; returns the retain-until date."""
        until = (datetime.now(timezone.utc) + timedelta(days=self._days)).replace(microsecond=0)

        def _put():
            self._client.put_object(
                self._bucket, key, io.BytesIO(data), len(data), content_type=content_type,
                retention=Retention(COMPLIANCE, until),
            )

        await asyncio.to_thread(_put)
        logger.info("audit_archive_stored", key=key, retain_until=until.isoformat())
        return until

    async def exists(self, key: str) -> bool:
        def _stat():
            try:
                self._client.stat_object(self._bucket, key)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_stat)
