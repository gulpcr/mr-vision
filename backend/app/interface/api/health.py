import asyncio
import time

import structlog
from fastapi import APIRouter

from app.config import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


async def _check_db() -> dict:
    """Check PostgreSQL connectivity."""
    from app.infrastructure.database.session import async_session_factory

    start = time.monotonic()
    try:
        async with async_session_factory() as session:
            await session.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def _check_redis() -> dict:
    """Check Redis connectivity."""
    import redis.asyncio as aioredis

    settings = get_settings()
    start = time.monotonic()
    try:
        client = aioredis.Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=3)
        try:
            await client.ping()
            return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
        finally:
            await client.aclose()
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def _check_minio() -> dict:
    """Check MinIO connectivity."""
    start = time.monotonic()
    try:
        from app.infrastructure.storage.client import get_artifact_store

        store = get_artifact_store()
        await asyncio.to_thread(store._client.bucket_exists, store._bucket)
        return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


@router.get("/health")
async def health():
    """Deep health check — verifies DB, Redis, and MinIO connectivity."""
    db, redis_check, minio = await asyncio.gather(
        _check_db(),
        _check_redis(),
        _check_minio(),
        return_exceptions=False,
    )

    services = {"database": db, "redis": redis_check, "minio": minio}
    all_ok = all(s["status"] == "ok" for s in services.values())

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "version": "1.0.0",
            "services": services,
        },
    )
