"""Cross-process, tenant-addressed realtime events (Redis pub/sub).

Events originate in the Celery worker as well as the API, but browser WebSockets are held
by the API processes (several uvicorn workers). Every event is therefore published to one
Redis channel tagged with its tenant; each API process subscribes and delivers it only to
its own sockets of that tenant (interface/api/ws.py). An event without a tenant is never
published — there is no broadcast-to-everyone path.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

CHANNEL = "ws:tenant-events"


# How long one pub/sub poll waits for an event, and how often an idle subscription is
# PINGed so a dead Redis connection is noticed.
_POLL_SECONDS = 10.0
_HEALTH_CHECK_SECONDS = 15


def _client() -> aioredis.Redis:
    # Fresh per call: redis.asyncio binds its pool to the running event loop, and Celery
    # tasks run each in a new loop (see infrastructure/ratelimit/redis_limiter.py).
    settings = get_settings()
    return aioredis.Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=3)


def _listener_client() -> aioredis.Redis:
    # No read timeout: a subscription is idle whenever no event is published, and with
    # socket_timeout=3 every quiet 3 s raised TimeoutError, tore the subscription down
    # and dropped any event published while it resubscribed. Liveness is checked by the
    # periodic health-check PING instead.
    settings = get_settings()
    return aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        socket_connect_timeout=3,
        socket_timeout=None,
        socket_keepalive=True,
        health_check_interval=_HEALTH_CHECK_SECONDS,
    )


async def publish_tenant_event(tenant_id: str, message: dict[str, Any]) -> bool:
    """Publish ``message`` to ``tenant_id``'s sockets. Returns False (logged) on failure —
    realtime notification is best-effort and must never fail the caller."""
    if not tenant_id:
        logger.warning("realtime_event_dropped_no_tenant", type=message.get("type"))
        return False
    client = _client()
    try:
        await client.publish(CHANNEL, json.dumps({"tenant_id": tenant_id, "message": message}))
        return True
    except Exception as exc:
        logger.warning("realtime_publish_failed", error=str(exc), type=message.get("type"))
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def listen_tenant_events(
    deliver: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    """Run forever: deliver each published event via ``deliver(tenant_id, message)``.
    Reconnects with backoff if Redis goes away; cancel the task to stop."""
    backoff = 1.0
    while True:
        client = _listener_client()
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(CHANNEL)
            backoff = 1.0
            while True:
                # Returns None when nothing arrives within the poll window (not an error);
                # each call also runs the health-check PING when it is due.
                item = await pubsub.get_message(timeout=_POLL_SECONDS)
                if item is None or item.get("type") != "message":
                    continue
                try:
                    envelope = json.loads(item["data"])
                    tenant_id = envelope.get("tenant_id")
                    message = envelope.get("message")
                    if tenant_id and isinstance(message, dict):
                        await deliver(tenant_id, message)
                except Exception as exc:
                    logger.warning("realtime_event_malformed", error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("realtime_listener_reconnecting", error=str(exc), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            try:
                await pubsub.aclose()
                await client.aclose()
            except Exception:
                pass
