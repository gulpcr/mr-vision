from __future__ import annotations

"""Password-login lockout and per-IP throttling (HIPAA 164.312(d)).

Same shape as ``mfa_lockout``: a Redis counter per account that expires
``login_lockout_minutes`` after the first failure, plus a fixed-window per-IP limit.
Like the other Redis helpers it fails OPEN (a Redis outage must not lock everyone out of
a clinical system); the outage itself is visible in /health.
"""

import redis.asyncio as aioredis

from app.infrastructure.redis_conn import async_redis
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

_ACCOUNT_PREFIX = "login:fail:"
_IP_PREFIX = "login:ip:"


def _client() -> aioredis.Redis:
    return async_redis(socket_timeout=3)


def _account_key(tenant_id: str | None, username: str) -> str:
    return f"{_ACCOUNT_PREFIX}{tenant_id or '*'}:{username.strip().lower()}"


async def is_locked(tenant_id: str | None, username: str) -> tuple[bool, int]:
    """(locked, retry_after_seconds) for this account."""
    settings = get_settings()
    key = _account_key(tenant_id, username)
    try:
        client = _client()
        count = int(await client.get(key) or 0)
        if count < settings.login_max_attempts:
            return False, 0
        ttl = await client.ttl(key)
        return True, max(int(ttl), 1)
    except Exception as exc:
        logger.warning("login_lockout_check_failed", error=str(exc))
        return False, 0


async def record_failure(tenant_id: str | None, username: str) -> int:
    """Count a failed password; returns the running count (0 if Redis is down)."""
    settings = get_settings()
    key = _account_key(tenant_id, username)
    try:
        client = _client()
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, int(settings.login_lockout_minutes * 60))
        return count
    except Exception as exc:
        logger.warning("login_lockout_record_failed", error=str(exc))
        return 0


async def clear_failures(tenant_id: str | None, username: str) -> None:
    try:
        await _client().delete(_account_key(tenant_id, username))
    except Exception as exc:
        logger.warning("login_lockout_clear_failed", error=str(exc))


async def ip_allowed(ip: str) -> bool:
    """Fixed one-minute window of login attempts per client IP."""
    if not ip:
        return True
    settings = get_settings()
    key = f"{_IP_PREFIX}{ip}"
    try:
        client = _client()
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, 60)
        return count <= settings.login_ip_attempts_per_minute
    except Exception as exc:
        logger.warning("login_ip_limit_failed", error=str(exc))
        return True
