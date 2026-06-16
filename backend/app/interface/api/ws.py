from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])

# Simple in-memory connection manager
_connections: list[WebSocket] = []


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("ws_connected", total=len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info("ws_disconnected", total=len(self.active_connections))

    async def broadcast(self, message: dict[str, Any]):
        """Send a message to all connected clients."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

    async def send_to_tenant(self, tenant_id: str, message: dict[str, Any]):
        """Send to all connections for a specific tenant."""
        # For now, broadcast to all - tenant filtering via client state
        await self.broadcast(message)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time notifications.

    Messages are JSON objects with 'type' field:
    - job_update: Job status changed
    - result_ready: New result available
    - alert: Alert triggered
    - batch_progress: Batch upload progress
    """
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, receive pings
            data = await websocket.receive_text()
            # Client can send subscribe/unsubscribe messages
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def notify_job_update(job_id: str, status: str, progress: float, study_uid: str):
    """Notify all clients about a job status change."""
    await manager.broadcast({
        "type": "job_update",
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "study_instance_uid": study_uid,
    })


async def notify_result_ready(study_uid: str, usecase_name: str, result_id: str):
    """Notify all clients about a new result."""
    await manager.broadcast({
        "type": "result_ready",
        "study_instance_uid": study_uid,
        "usecase_name": usecase_name,
        "result_id": result_id,
    })


async def notify_alert(event_type: str, payload: dict[str, Any]):
    """Notify clients about a triggered alert."""
    await manager.broadcast({
        "type": "alert",
        "event_type": event_type,
        "payload": payload,
    })


async def notify_batch_progress(batch_id: str, completed: int, total: int, status: str):
    """Notify clients about batch upload progress."""
    await manager.broadcast({
        "type": "batch_progress",
        "batch_id": batch_id,
        "completed": completed,
        "total": total,
        "status": status,
    })
