"""Patient onboarding API (intake).

GET  /patients?query=     require(study.view)      search de-identified patients
POST /orders {...}         require(patient.onboard)  upsert patient + create order (txn)
POST /orders/{id}/link-study?study_uid=  require(patient.onboard)  link an ingested study
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.onboarding_service import OnboardingService, OnboardingValidationError
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(tags=["onboarding"])


class OrderCreate(BaseModel):
    patient_ref: str = Field(..., min_length=1, max_length=128)
    sex: str = Field(..., description="female | male | other")
    age_band: str = Field(..., description="0-17 | 18-39 | 40-64 | 65+")
    modality: str = Field(..., min_length=1, max_length=16)
    body_part: str | None = Field(None, max_length=64)
    indication: str = Field(..., min_length=1)
    region_profile: str = Field(..., min_length=1, max_length=64)
    referrer: str | None = None
    priority: str = "routine"
    consent_ack: bool = False
    study_instance_uid: str | None = None
    clinical_history: str | None = None
    comparative_study: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    fasting_glucose: str | None = None
    injection_site: str | None = None
    creatinine: str | None = None


class PatientUpdate(BaseModel):
    sex: str | None = None
    age_band: str | None = None


class OrderUpdate(BaseModel):
    modality: str | None = None
    body_part: str | None = None
    indication: str | None = None
    region_profile: str | None = None
    referrer: str | None = None
    priority: str | None = None
    consent_ack: bool | None = None
    study_instance_uid: str | None = None
    clinical_history: str | None = None
    comparative_study: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    fasting_glucose: str | None = None
    injection_site: str | None = None
    creatinine: str | None = None


def _actor_id(request: Request) -> str:
    return getattr(request.state, "user_id", "") or ""


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


@router.get("/patients", dependencies=[require_permission("study.view")])
async def search_patients(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    query: str = Query("", description="Filter by patient_ref (MRN) substring"),
):
    return await OnboardingService(session).list_patients(query=query, tenant_id=_tenant(request))


@router.get("/studies/{study_uid}/clinical", dependencies=[require_permission("study.view")])
async def get_study_clinical(
    study_uid: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Clinical intake fields for a study (for the report view). {} if none linked."""
    clinical = await OnboardingService(session).get_clinical_for_study(study_uid)
    return clinical or {}


@router.get("/patients/{patient_id}", dependencies=[require_permission("study.view")])
async def get_patient(
    patient_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await OnboardingService(session).get_patient(patient_id, tenant_id=_tenant(request))
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return result


@router.patch("/patients/{patient_id}", dependencies=[require_permission("patient.onboard")])
async def update_patient(
    patient_id: str,
    body: PatientUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        result = await OnboardingService(session).update_patient(
            patient_id, body.model_dump(exclude_unset=True),
            actor_id=_actor_id(request), tenant_id=_tenant(request),
        )
    except OnboardingValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    await session.commit()
    return result


@router.patch("/orders/{order_id}", dependencies=[require_permission("patient.onboard")])
async def update_order(
    order_id: str,
    body: OrderUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        result = await OnboardingService(session).update_order(
            order_id, body.model_dump(exclude_unset=True),
            actor_id=_actor_id(request), tenant_id=_tenant(request),
        )
    except OnboardingValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Order not found")
    await session.commit()
    return result


@router.post("/orders", status_code=201, dependencies=[require_permission("patient.onboard")])
async def create_order(
    body: OrderCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        result = await OnboardingService(session).create_order(
            body.model_dump(), actor_id=_actor_id(request), tenant_id=_tenant(request)
        )
    except OnboardingValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return result


@router.post("/orders/{order_id}/link-study", dependencies=[require_permission("patient.onboard")])
async def link_study(
    order_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    study_uid: str = Query(...),
):
    try:
        result = await OnboardingService(session).link_study(
            order_id, study_uid, actor_id=_actor_id(request), tenant_id=_tenant(request)
        )
    except OnboardingValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Order not found")
    await session.commit()
    return result
