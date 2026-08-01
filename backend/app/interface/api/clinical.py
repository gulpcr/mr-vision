"""Clinical data API — the FHIR search axes for Observation and Condition (step 9).

These endpoints expose the four search parameters the FHIR ``Observation`` contract is
built around — ``patient``, ``code``, ``category``, ``date`` — plus ``Condition``'s
``patient`` / ``code`` / ``clinical-status``. Query names use the FHIR spellings
(``patient``, ``clinical-status``) so the mapping to a real FHIR façade is mechanical, and
date bounds use the ``ge`` / ``le`` semantics FHIR defines rather than inventing new ones.

Why this is a conformance item and not a convenience: before the ``observations`` table
these values lived inside an opaque JSON blob, so "every SUVmax for this patient over two
years" was not a query the platform could answer at all. Having the rows without exposing
the axes would leave that half-finished.

Responses are the platform's own JSON, not FHIR resources. That is deliberate: emitting
``Observation`` resources externally requires the terminology sign-off that
``fhir_map.yaml`` still marks ``<TBD-verify>``, and shipping resources coded with local
codes would invite consumers to treat them as standard. The rows carry ``code_system`` so
a caller can see exactly which vocabulary each code belongs to.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/clinical", tags=["clinical"])


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


def _observation_dict(o: Any) -> dict[str, Any]:
    return {
        "id": o.id,
        "patient_ref": o.patient_ref,
        "study_instance_uid": o.study_instance_uid,
        "result_id": o.result_id,
        "category": o.category,
        "code_system": o.code_system,
        "code": o.code,
        "code_display": o.code_display,
        "value_quantity": o.value_quantity,
        "value_unit": o.value_unit,
        "value_string": o.value_string,
        "value_codeable_code": o.value_codeable_code,
        "value_codeable_system": o.value_codeable_system,
        "body_site_code": o.body_site_code,
        "laterality": o.laterality,
        "status": o.status,
        "effective_dt": o.effective_dt.isoformat() if o.effective_dt else None,
        "issued": o.issued.isoformat() if o.issued else None,
        "derived_from_device": o.derived_from_device,
    }


def _condition_dict(c: Any) -> dict[str, Any]:
    return {
        "id": c.id,
        "patient_ref": c.patient_ref,
        "study_instance_uid": c.study_instance_uid,
        "code_system": c.code_system,
        "code": c.code,
        "code_display": c.code_display,
        "category": c.category,
        "clinical_status": c.clinical_status,
        "verification_status": c.verification_status,
        "onset_dt": c.onset_dt.isoformat() if c.onset_dt else None,
        "recorded_dt": c.recorded_dt.isoformat() if c.recorded_dt else None,
        "note": c.note,
    }


@router.get("/observations", dependencies=[require_permission("study.view")])
async def search_observations(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    patient: str | None = Query(None, description="Patient MRN (FHIR: patient)"),
    code: str | None = Query(None, description="Observation code (FHIR: code)"),
    category: str | None = Query(
        None, description="vital-signs | laboratory | imaging | survey (FHIR: category)"
    ),
    date_ge: datetime | None = Query(
        None, alias="date-ge", description="effectiveDateTime >= (FHIR: date=ge...)"
    ),
    date_le: datetime | None = Query(
        None, alias="date-le", description="effectiveDateTime <= (FHIR: date=le...)"
    ),
    study: str | None = Query(None, description="Study Instance UID"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Search Observations — the canonical patient/code/category/date axes.

    Backed by the ``ix_observations_search`` composite index on
    ``(tenant_id, patient_ref, code, effective_dt)``, so the common query shape is one
    index scan rather than a scan plus JSON extraction.
    """
    from app.application.observation_service import ObservationService

    rows = await ObservationService(session).search(
        patient_ref=patient,
        code=code,
        category=category,
        date_from=date_ge,
        date_to=date_le,
        study_instance_uid=study,
        tenant_id=_tenant(request),
        limit=limit,
    )
    return {"total": len(rows), "observations": [_observation_dict(r) for r in rows]}


@router.get("/conditions", dependencies=[require_permission("study.view")])
async def search_conditions(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    patient: str | None = Query(None, description="Patient MRN (FHIR: patient)"),
    code: str | None = Query(None, description="ICD-10 / SNOMED code (FHIR: code)"),
    clinical_status: str | None = Query(
        None, alias="clinical-status", description="active | resolved | ... "
    ),
    category: str | None = Query(
        None, description="problem-list-item | encounter-diagnosis"
    ),
    limit: int = Query(200, ge=1, le=1000),
):
    """Search coded Conditions by patient / code / clinical-status / category."""
    from app.application.condition_service import ConditionService

    rows = await ConditionService(session).search(
        patient_ref=patient,
        code=code,
        clinical_status=clinical_status,
        category=category,
        tenant_id=_tenant(request),
        limit=limit,
    )
    return {"total": len(rows), "conditions": [_condition_dict(r) for r in rows]}
