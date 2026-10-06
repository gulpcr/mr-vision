from __future__ import annotations

"""Patient rights endpoints: right of access (HIPAA 164.524) and the access /
disclosure report (164.528). Both are workspace-scoped and audited."""

import csv
import io
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.patient_access_service import (
    PatientAccessService,
    PatientNotFound,
    WeakPassphrase,
)
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/patients", tags=["patient-rights"])


class AccessExportRequest(BaseModel):
    requested_by: str = Field(min_length=3, max_length=256,
                              description="Who requested the copy (the patient or their representative)")
    include_images: bool = False
    # Optional: AES-256 encrypt the ZIP (for a copy handed over off-platform). Given to
    # the patient separately; never stored or logged.
    passphrase: str | None = Field(None, max_length=256)


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


@router.post("/{mrn}/access-export", dependencies=[require_permission("patient.rights")])
async def export_patient_record(
    mrn: str,
    body: AccessExportRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Right of access: a ZIP with the patient's studies, results, report PDFs, AI
    images and (optionally) the original DICOM. Recorded as a disclosure."""
    from app.infrastructure.orthanc.client import OrthancPACSClient
    from app.infrastructure.storage.client import get_artifact_store

    pacs = OrthancPACSClient() if body.include_images else None
    try:
        store = get_artifact_store()
    except Exception:  # object store unreachable: export the rest, note it in the manifest
        store = None
    try:
        service = PatientAccessService(session, _tenant(request), store, pacs)
        data, _counts = await service.export_record(
            mrn, requester=body.requested_by, actor=getattr(request.state, "user", "unknown"),
            include_images=body.include_images, passphrase=body.passphrase or None,
        )
    except WeakPassphrase as e:
        raise HTTPException(status_code=422, detail=str(e))
    except PatientNotFound:
        raise HTTPException(status_code=404, detail="No studies for this MRN in your workspace")
    finally:
        if pacs is not None:
            await pacs.close()
    await session.commit()
    safe = "".join(c for c in mrn if c.isalnum() or c in "-_")[:40] or "patient"
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="patient_record_{safe}.zip"'},
    )


@router.get("/{mrn}/access-report", dependencies=[require_permission("patient.rights")])
async def patient_access_report(
    mrn: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int = Query(default=6 * 365, ge=1, le=10 * 365),
    format: str = Query(default="json", pattern="^(json|csv)$"),
):
    """Accounting of disclosures + access report: who accessed or received this
    patient's data, when and from where (from the tamper-evident audit log)."""
    from app.application.audit_service import AuditService

    try:
        report = await PatientAccessService(session, _tenant(request)).access_report(mrn, days)
    except PatientNotFound:
        raise HTTPException(status_code=404, detail="No studies for this MRN in your workspace")
    await AuditService(session).record_read(
        request, "access_report_generated", "patient", mrn, details={"days": days, "format": format},
    )
    if format == "json":
        return report
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["kind", "timestamp", "action", "user", "entity_type", "entity_id", "route", "client_ip"])
    for kind in ("disclosures", "accesses"):
        for e in report[kind]:
            writer.writerow([kind[:-1], e["timestamp"], e["action"], e["user"], e["entity_type"],
                             e["entity_id"], e["route"] or "", e["client_ip"] or ""])
    safe = "".join(c for c in mrn if c.isalnum() or c in "-_")[:40] or "patient"
    return Response(
        content=out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="access_report_{safe}.csv"'},
    )


# ── Request register and 164.522 restrictions (PRV-04) ────────────────────────
# Own prefix: /patients/{id} belongs to the onboarding router.

rights_router = APIRouter(prefix="/patient-rights", tags=["patient-rights"])


class PatientRequestIn(BaseModel):
    mrn: str = Field(min_length=1, max_length=64)
    request_type: str = Field(pattern="^(access|amendment|accounting|restriction|confidential_communication)$")
    requester: str = Field(min_length=3, max_length=256)
    details: str | None = Field(None, max_length=8000)
    received_at: datetime | None = None


class ExtendIn(BaseModel):
    reason: str = Field(min_length=3, max_length=4000)


class CloseIn(BaseModel):
    outcome: str = Field(pattern="^(fulfilled|denied)$")
    notes: str = Field("", max_length=8000)


class RestrictionIn(BaseModel):
    mrn: str = Field(min_length=1, max_length=64)
    channel: str = Field(pattern="^(fhir|dicom_export|webhooks|share_links|all)$")
    reason: str = Field(min_length=3, max_length=4000)
    expires_at: datetime | None = None
    request_id: str | None = None


def _actor(request: Request) -> str:
    return getattr(request.state, "user", None) or "unknown"


def _rights(request: Request, session: AsyncSession):
    from app.application.patient_rights_service import PatientRightsService

    return PatientRightsService(session, _tenant(request))


async def _call(session: AsyncSession, coro):
    from app.application.patient_rights_service import RequestNotFound
    from app.domain.patient_rights import InvalidRequest

    try:
        out = await coro
    except InvalidRequest as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RequestNotFound:
        raise HTTPException(status_code=404, detail="Not found")
    await session.commit()
    return out


@rights_router.get("/requests", dependencies=[require_permission("patient.rights")])
async def list_patient_requests(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    status: str | None = Query(None, pattern="^(open|overdue|extended|fulfilled|denied)$"),
    mrn: str | None = Query(None, max_length=64),
):
    return await _rights(request, session).list_requests(status, mrn)


@rights_router.post("/requests", status_code=201, dependencies=[require_permission("patient.rights")])
async def log_patient_request(body: PatientRequestIn, request: Request,
                              session: Annotated[AsyncSession, Depends(get_session)]):
    return await _call(session, _rights(request, session).create_request(
        body.mrn, body.request_type, body.requester, _actor(request), body.details, body.received_at,
    ))


@rights_router.post("/requests/{request_id}/extend", dependencies=[require_permission("patient.rights")])
async def extend_patient_request(request_id: str, body: ExtendIn, request: Request,
                                 session: Annotated[AsyncSession, Depends(get_session)]):
    return await _call(session, _rights(request, session).extend_request(
        request_id, body.reason, _actor(request)))


@rights_router.post("/requests/{request_id}/close", dependencies=[require_permission("patient.rights")])
async def close_patient_request(request_id: str, body: CloseIn, request: Request,
                                session: Annotated[AsyncSession, Depends(get_session)]):
    return await _call(session, _rights(request, session).close_request(
        request_id, body.outcome, body.notes, _actor(request)))


@rights_router.get("/restrictions", dependencies=[require_permission("patient.rights")])
async def list_restrictions(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    mrn: str | None = Query(None, max_length=64),
    active: bool = False,
):
    return await _rights(request, session).list_restrictions(mrn, active)


@rights_router.post("/restrictions", status_code=201, dependencies=[require_permission("patient.rights")])
async def add_restriction(body: RestrictionIn, request: Request,
                          session: Annotated[AsyncSession, Depends(get_session)]):
    return await _call(session, _rights(request, session).add_restriction(
        body.mrn, body.channel, body.reason, _actor(request), body.expires_at, body.request_id))


@rights_router.post("/restrictions/{restriction_id}/revoke", dependencies=[require_permission("patient.rights")])
async def revoke_restriction(restriction_id: str, request: Request,
                             session: Annotated[AsyncSession, Depends(get_session)]):
    return await _call(session, _rights(request, session).revoke_restriction(
        restriction_id, _actor(request)))
