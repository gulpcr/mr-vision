from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_ATTEMPTS_PREFIX = "mfa:attempts:"


def _get_client() -> aioredis.Redis:
    # Deliberately NOT a module-level singleton — see redis_limiter.py /
    # impersonation_blocklist.py for why: redis.asyncio.Redis binds its
    # connection pool to whichever event loop is running when first used,
    # and a cached client reused across a different loop either raises or
    # silently no-ops. Fresh per call is cheap (no eager socket open).
    settings = get_settings()
    return aioredis.Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=3)


async def record_failure(user_id: str) -> int:
    """Increments the failed-attempt counter (fixed window = the lockout
    duration itself) and returns the new count. Fails open (returns 0, i.e.
    "no failures recorded") on Redis errors — see is_locked() for why an
    auth-adjacent outage must not turn into a platform-wide lockout.
    """
    settings = get_settings()
    client = _get_client()
    try:
        key = f"{_ATTEMPTS_PREFIX}{user_id}"
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, max(1, int(settings.mfa_lockout_minutes * 60)))
        return count
    except Exception:
        return 0
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def clear_failures(user_id: str) -> None:
    """Reset on a successful verification — a legitimate login shouldn't
    carry over a near-miss attempt count from before."""
    client = _get_client()
    try:
        await client.delete(f"{_ATTEMPTS_PREFIX}{user_id}")
    except Exception:
        pass
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def is_locked(user_id: str) -> tuple[bool, int]:
    """Returns (locked, retry_after_seconds). Fails OPEN (not locked) on
    Redis errors — this check runs on every MFA verification attempt
    (including the pre-auth login step), and a Redis outage must not turn
    into every account being un-verifiable platform-wide. Same posture as
    impersonation_blocklist.is_blocked().
    """
    settings = get_settings()
    client = _get_client()
    try:
        key = f"{_ATTEMPTS_PREFIX}{user_id}"
        count = await client.get(key)
        if count is None or int(count) < settings.mfa_max_attempts:
            return False, 0
        ttl = await client.ttl(key)
        return True, max(ttl, 0)
    except Exception:
        return False, 0
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
