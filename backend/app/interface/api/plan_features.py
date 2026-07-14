from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.application.plan_service import PlanService
from app.interface.api.dependencies import get_plan_service
from app.interface.middleware.auth import require_platform_admin

router = APIRouter(
    prefix="/admin/plans", tags=["plans"], dependencies=[Depends(require_platform_admin)],
)


class SetPlanFeaturesRequest(BaseModel):
    features: list[str] = Field(default_factory=list)


@router.get("")
async def list_all_plans(
    service: Annotated[PlanService, Depends(get_plan_service)],
):
    return await service.list_all_plans()


@router.get("/{plan_name}/features")
async def get_plan_features(
    plan_name: str,
    service: Annotated[PlanService, Depends(get_plan_service)],
):
    return {"plan_name": plan_name, "features": await service.get_plan_features(plan_name)}


@router.put("/{plan_name}/features")
async def set_plan_features(
    plan_name: str,
    body: SetPlanFeaturesRequest,
    service: Annotated[PlanService, Depends(get_plan_service)],
):
    features = await service.set_plan_features(plan_name, body.features)
    return {"plan_name": plan_name, "features": features}
