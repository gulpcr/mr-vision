from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings


def _get_client() -> aioredis.Redis:
    # Deliberately NOT a module-level singleton: redis.asyncio.Redis binds its
    # connection pool to whichever asyncio event loop is running when it's
    # first used. A cached client reused across a different loop (e.g. a
    # Celery task's fresh event loop per run, or multiple test event loops)
    # either raises or silently no-ops — and since this fails open on any
    # exception below, that would surface as "rate limiting silently doesn't
    # work" rather than a visible error. Constructing fresh per call is cheap:
    # redis.asyncio.Redis() does not eagerly open a socket, only the
    # connection pool's first real command does.
    settings = get_settings()
    return aioredis.Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=3)


async def check_rate_limit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """Fixed-window counter. Returns True if the caller is within limit.

    Fails open on Redis errors — a rate limiter outage must never block uploads.
    """
    client = _get_client()
    try:
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
        return count <= limit
    except Exception:
        return True
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
