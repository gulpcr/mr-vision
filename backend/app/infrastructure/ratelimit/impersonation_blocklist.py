from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_KEY_PREFIX = "impersonation:blocked:"


def _get_client() -> aioredis.Redis:
    # Deliberately NOT a module-level singleton: redis.asyncio.Redis binds its
    # connection pool to whichever asyncio event loop is running when it's
    # first used. A cached client reused across a different loop (e.g. each
    # of Starlette TestClient's synchronous calls can run its own loop) either
    # raises or silently no-ops — and since is_blocked() below fails open on
    # any exception (a genuine Redis outage must not break every request),
    # that would surface as "revocation silently didn't work" rather than a
    # visible error. Constructing fresh per call is cheap: redis.asyncio.Redis()
    # does not eagerly open a socket, only the connection pool's first real
    # command does.
    settings = get_settings()
    return aioredis.Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=3)


async def block_jti(jti: str, ttl_seconds: int) -> None:
    """Revoke a single impersonation token immediately (the /impersonate/stop
    endpoint). ttl_seconds should be the token's remaining lifetime — no point
    keeping a blocklist entry alive longer than the token itself would be.

    Fails open on Redis errors is NOT acceptable here (unlike rate limiting):
    if we can't record the block, the token stays valid, so a failed stop
    must be surfaced to the caller rather than silently "succeeding".
    """
    if ttl_seconds <= 0:
        return
    client = _get_client()
    try:
        await client.set(f"{_KEY_PREFIX}{jti}", "1", ex=ttl_seconds)
    finally:
        await client.aclose()


async def is_blocked(jti: str) -> bool:
    """Fails OPEN (treats Redis errors as "not blocked") — this check runs on
    every request carrying an impersonation token, and an auth-adjacent outage
    must not turn into a full platform outage. The trade-off: if Redis is down
    at the exact moment someone tries to use a just-stopped impersonation
    token, it's accepted rather than rejected, same posture as the existing
    rate limiter in redis_limiter.py.
    """
    client = _get_client()
    try:
        return bool(await client.exists(f"{_KEY_PREFIX}{jti}"))
    except Exception:
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
