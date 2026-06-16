from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.interface.api.dependencies import get_session as get_async_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/critical-alerts", tags=["critical-alerts"])


class AcknowledgeRequest(BaseModel):
    acknowledged_by: str


@router.get("")
async def list_critical_alerts(
    status: str | None = Query(None, description="pending|acknowledged|escalated|resolved"),
    severity: str | None = Query(None, description="CRITICAL|WARNING"),
    usecase_name: str | None = Query(None),
    patient_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    from app.application.alerting_service import AlertingService

    svc = AlertingService(session)
    alerts = await svc.list_critical_alerts(
        status=status,
        severity=severity,
        usecase_name=usecase_name,
        patient_id=patient_id,
        limit=limit,
        offset=offset,
    )
    return {"alerts": alerts, "count": len(alerts)}


@router.get("/stats")
async def get_critical_alert_stats(
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    from app.application.alerting_service import AlertingService

    svc = AlertingService(session)
    return await svc.get_critical_alert_stats()


@router.get("/{alert_id}")
async def get_critical_alert(
    alert_id: str,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    from app.application.alerting_service import AlertingService

    svc = AlertingService(session)
    alert = await svc.get_critical_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@router.post("/{alert_id}/acknowledge")
async def acknowledge_critical_alert(
    alert_id: str,
    body: AcknowledgeRequest,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    from app.application.alerting_service import AlertingService

    svc = AlertingService(session)
    alert = await svc.acknowledge_critical_alert(alert_id, body.acknowledged_by)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    await session.commit()
    logger.info("critical_alert_acknowledged", alert_id=alert_id, by=body.acknowledged_by)
    return alert
