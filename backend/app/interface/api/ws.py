from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.infrastructure.realtime.events import publish_tenant_event
from app.interface.api.viewer_access import resolve_viewer

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])

# Close code for a rejected handshake (RFC 6455 1008 = policy violation).
# Application close code for "no valid viewer session". The socket is accepted and then
# closed with it: a handshake rejected before accept() reaches the browser only as an
# HTTP 403 / close code 1006, indistinguishable from a network drop, so the client kept
# retrying every 30 s forever. 4401 tells it to stop until the session is renewed.
_WS_UNAUTHORIZED = 4401


class ConnectionManager:
    """This process's WebSocket connections, grouped by tenant.

    There is deliberately no broadcast-to-everyone: every event is addressed to one
    tenant and only reaches sockets authenticated as that tenant.
    """

    def __init__(self):
        self._by_tenant: dict[str, list[WebSocket]] = {}

    @property
    def active_connections(self) -> list[WebSocket]:
        return [ws for conns in self._by_tenant.values() for ws in conns]

    async def connect(self, websocket: WebSocket, tenant_id: str):
        await websocket.accept()
        self._by_tenant.setdefault(tenant_id, []).append(websocket)
        logger.info("ws_connected", tenant_id=tenant_id, total=len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        for tenant_id, conns in list(self._by_tenant.items()):
            if websocket in conns:
                conns.remove(websocket)
                if not conns:
                    del self._by_tenant[tenant_id]
        logger.info("ws_disconnected", total=len(self.active_connections))

    async def send_to_tenant(self, tenant_id: str, message: dict[str, Any]):
        """Deliver to this process's sockets of ``tenant_id`` only."""
        disconnected = []
        for connection in list(self._by_tenant.get(tenant_id, [])):
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time notifications.

    Authenticated by the viewer-session cookie set at login (browsers cannot attach an
    Authorization header to a WebSocket) — HTTP middleware never sees WebSocket
    handshakes, so this endpoint checks it itself. The socket only ever receives its own
    tenant's events.

    Messages are JSON objects with 'type' field:
    - job_update: Job status changed
    - result_ready: New result available
    - alert: Alert triggered
    - batch_progress: Batch upload progress
    """
    viewer = resolve_viewer(websocket)
    if viewer is None:
        await websocket.accept()
        await websocket.close(code=_WS_UNAUTHORIZED, reason="viewer session required")
        return

    await manager.connect(websocket, viewer.tenant_id)
    try:
        while True:
            # Keep connection alive, receive pings
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def _notify(tenant_id: str, message: dict[str, Any]) -> None:
    """Publish via Redis so every API process delivers it; if Redis is unavailable,
    still deliver to this process's own sockets of the tenant."""
    if not await publish_tenant_event(tenant_id, message):
        await manager.send_to_tenant(tenant_id, message)


async def notify_job_update(
    tenant_id: str, job_id: str, status: str, progress: float, study_uid: str
):
    """Notify the tenant's clients about a job status change."""
    await _notify(tenant_id, {
        "type": "job_update",
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "study_instance_uid": study_uid,
    })


async def notify_result_ready(tenant_id: str, study_uid: str, usecase_name: str, result_id: str):
    """Notify the tenant's clients about a new result."""
    await _notify(tenant_id, {
        "type": "result_ready",
        "study_instance_uid": study_uid,
        "usecase_name": usecase_name,
        "result_id": result_id,
    })


async def notify_alert(tenant_id: str, event_type: str, payload: dict[str, Any]):
    """Notify the tenant's clients about a triggered alert."""
    await _notify(tenant_id, {
        "type": "alert",
        "event_type": event_type,
        "payload": payload,
    })


async def notify_batch_progress(
    tenant_id: str, batch_id: str, completed: int, total: int, status: str
):
    """Notify the tenant's clients about batch upload progress."""
    await _notify(tenant_id, {
        "type": "batch_progress",
        "batch_id": batch_id,
        "completed": completed,
        "total": total,
        "status": status,
    })
