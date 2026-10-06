from __future__ import annotations

"""The one place a Redis client is constructed (password + optional TLS from settings).

Deliberately not a module-level singleton: redis.asyncio.Redis binds its connection pool
to the event loop it first runs on, and this code runs under several loops (FastAPI,
Celery tasks, tests). Construction is cheap — no socket is opened until the first
command.
"""

import redis.asyncio as aioredis

from app.infrastructure.tls import redis_kwargs


def async_redis(**overrides) -> aioredis.Redis:
    """An asyncio Redis client. ``overrides`` (e.g. socket_timeout) win over defaults."""
    kwargs = {"socket_timeout": 3, **redis_kwargs(), **overrides}
    return aioredis.Redis(**kwargs)
