from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from typing import Any

import structlog
from minio import Minio
from minio.error import S3Error

from app.config import get_settings
from app.domain.interfaces import ArtifactStore

logger = structlog.get_logger(__name__)

_instance: MinIOArtifactStore | None = None


class MinIOArtifactStore(ArtifactStore):
    """MinIO-backed artifact storage for segmentation masks, overlays, and reports.

    All blocking MinIO SDK calls are dispatched to a thread pool via
    ``asyncio.to_thread`` so the event loop is never blocked.
    """

    def __init__(self):
        settings = get_settings()
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self._bucket = settings.minio_bucket
        self._ensure_bucket()

    def _ensure_bucket(self):
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info("created_minio_bucket", bucket=self._bucket)

    async def put(
        self, path: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        def _put():
            stream = io.BytesIO(data)
            self._client.put_object(
                self._bucket,
                path,
                stream,
                length=len(data),
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
        logger.info("stored_artifact", path=path, size=len(data))
        return path

    async def get(self, path: str) -> bytes:
        def _get():
            response = self._client.get_object(self._bucket, path)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    async def get_presigned_url(self, path: str, expires_secs: int = 3600) -> str:
        def _presign():
            return self._client.presigned_get_object(
                self._bucket, path, expires=timedelta(seconds=expires_secs)
            )

        return await asyncio.to_thread(_presign)

    async def exists(self, path: str) -> bool:
        def _exists():
            try:
                self._client.stat_object(self._bucket, path)
                return True
            except S3Error:
                return False

        return await asyncio.to_thread(_exists)

    async def delete(self, path: str) -> None:
        await asyncio.to_thread(self._client.remove_object, self._bucket, path)
        logger.info("deleted_artifact", path=path)

    async def put_file(self, path: str, file_path: str, content_type: str = "application/octet-stream") -> str:
        def _put_file():
            self._client.fput_object(
                self._bucket,
                path,
                file_path,
                content_type=content_type,
            )

        await asyncio.to_thread(_put_file)
        logger.info("stored_artifact_from_file", path=path, source=file_path)
        return path


def get_artifact_store() -> MinIOArtifactStore:
    """Return a module-level singleton MinIOArtifactStore.

    Avoids creating a new client (and calling ``bucket_exists``) on every
    request.
    """
    global _instance
    if _instance is None:
        _instance = MinIOArtifactStore()
    return _instance
