import asyncio
import re
from typing import Annotated

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import get_settings
from app.application.audit_service import AuditService, actor_from_request
from app.application.study_service import (
    StudyService,
    StudyTenantConflictError,
    StudyUnattributedError,
)
from app.application.usecase_registry import UseCaseRegistry
from app.infrastructure.orthanc.client import OrthancPACSClient
from app.application.job_orchestrator import JobOrchestrator
from app.application.routing_service import RoutingService
from app.infrastructure.tenant.db_scope import reapply_scope
from app.interface.api.dependencies import (
    build_job_orchestrator,
    build_study_service,
    get_registry,
    get_study_service,
    get_routing_service,
    get_session,
)
from app.interface.middleware.auth import require_permission
from app.interface.api.validators import validate_dicom_uid
from app.interface.schemas.study import (
    OrthancStableStudyNotification,
    SeriesResponse,
    StudyIngestRequest,
    StudyListResponse,
    StudyResponse,
)

from app.domain.permissions import STUDY_READ

router = APIRouter(prefix="/studies", tags=["studies"])


@router.get("", response_model=StudyListResponse, dependencies=[require_permission(STUDY_READ)])
async def list_studies(
    service: Annotated[StudyService, Depends(get_study_service)],
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    body_part: str | None = None,
    modality: str | None = None,
    patient_id: str | None = None,
):
    filters = {}
    if body_part:
        filters["body_part_examined"] = body_part.upper()
    if modality:
        filters["modality"] = modality.upper()
    if patient_id:
        filters["patient_id"] = patient_id

    studies, total = await service.list_studies(offset, limit, filters or None)
    return StudyListResponse(
        studies=[_to_response(s) for s in studies],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{study_uid}", response_model=StudyResponse, dependencies=[require_permission(STUDY_READ)])
async def get_study(
    study_uid: str,
    service: Annotated[StudyService, Depends(get_study_service)],
):
    validate_dicom_uid(study_uid)
    study = await service.get_study(study_uid)
    if not study:
        raise HTTPException(status_code=404, detail=f"Study {study_uid} not found")
    return _to_response(study)


@router.get("/{study_uid}/routing-preview", dependencies=[require_permission(STUDY_READ)])
async def routing_preview(
    study_uid: str,
    service: Annotated[StudyService, Depends(get_study_service)],
    routing: Annotated[RoutingService, Depends(get_routing_service)],
):
    """Dry-run auto-classification.

    Returns the use cases that WOULD be matched for this study (priority-ordered),
    plus every candidate with a per-use-case reason and the DICOM tags used — all
    without creating any jobs. Useful for verifying routing rules before ingest.
    """
    validate_dicom_uid(study_uid)
    study = await service.get_study(study_uid)
    if not study:
        raise HTTPException(status_code=404, detail=f"Study {study_uid} not found")
    return routing.preview_routing(study, study.series or [])


# Cap on files per request. Well above the client's batch size but bounded so a
# single request can't buffer an unlimited number of instances in memory. Raising
# Starlette's default (1000) is necessary because a DICOM series folder can exceed
# it — otherwise the whole request is rejected with 400 "Too many files" before our
# handler ever runs. The browser still uploads in small batches; this is the ceiling.
_MAX_UPLOAD_FILES = 5000


@router.post("/upload", dependencies=[require_permission("study.upload")])
async def upload_dicom_files(
    request: Request,
    service: Annotated[StudyService, Depends(get_study_service)],
):
    """Upload DICOM files (or a whole folder) from the browser and ingest them.

    Session-authed via the platform's RBACMiddleware (unlike the open Orthanc
    Explorer). Each file is stored in Orthanc and every distinct study it belongs
    to is ingested into the platform, ready for AI routing. Files that are not
    valid DICOM are reported per-file rather than failing the whole batch.

    The multipart form is parsed manually so the per-request file limit can be
    raised above Starlette's default of 1000 (a series folder routinely exceeds
    it). The browser uploads in small batches; ``_MAX_UPLOAD_FILES`` is the hard
    ceiling that keeps one request's memory bounded.
    """
    if not get_settings().dicom_upload_enabled:
        raise HTTPException(status_code=403, detail="In-app DICOM upload is disabled")

    try:
        form = await request.form(
            max_files=_MAX_UPLOAD_FILES, max_fields=_MAX_UPLOAD_FILES
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed upload: {exc}")

    uploads = [f for f in form.getlist("files") if isinstance(f, StarletteUploadFile)]
    if not uploads:
        raise HTTPException(status_code=400, detail="No files provided")

    payload = [(f.filename or "unnamed.dcm", await f.read()) for f in uploads]
    return await service.upload_and_ingest(payload)


@router.post("", response_model=StudyResponse, status_code=201, dependencies=[require_permission("study.upload")])
async def ingest_study(
    body: StudyIngestRequest,
    service: Annotated[StudyService, Depends(get_study_service)],
):
    try:
        study = await service.ingest_study(body.study_instance_uid)
    except StudyTenantConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_response(study)


@router.delete(
    "/{study_uid}",
    status_code=204,
    dependencies=[require_permission("study.delete")],
)
async def delete_study(
    study_uid: str,
    request: Request,
    service: Annotated[StudyService, Depends(get_study_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Remove a study and all related data from the caller's workspace.

    Tenant-scoped (a study in another workspace is reported as not found), gated on
    ``study.delete`` and audited. Cascades through the DB (series, job_runs,
    results_index) via FK constraints, then deletes every MinIO artifact stored under
    the study prefix. Orthanc PACS data is NOT touched — the DICOM files remain.
    """
    import structlog as _sl

    logger = _sl.get_logger(__name__)
    validate_dicom_uid(study_uid)

    if not await service.delete_study(study_uid):
        raise HTTPException(status_code=404, detail=f"Study {study_uid} not found")

    actor_id, actor_display, client_ip = actor_from_request(request)
    await AuditService(session).record(
        action="study_deleted",
        entity_type="study",
        entity_id=study_uid,
        actor_id=actor_id,
        actor_display=actor_display,
        client_ip=client_ip,
    )
    await session.commit()

    # Remove MinIO artifacts for this study (non-fatal — log warning on failure)
    try:
        from app.infrastructure.storage.client import get_artifact_store
        from minio.deleteobjects import DeleteObject

        store = get_artifact_store()

        from app.domain.storage_keys import study_prefixes

        tenant_prefix_id = getattr(request.state, "tenant_id", None) or "default"

        def _purge():
            objects = [
                o
                for prefix in study_prefixes(tenant_prefix_id, study_uid)
                for o in store._client.list_objects(store._bucket, prefix=prefix, recursive=True)
            ]
            if objects:
                errors = list(store._client.remove_objects(
                    store._bucket,
                    (DeleteObject(o.object_name) for o in objects),
                ))
                return len(objects), len(errors)
            return 0, 0

        deleted, errs = await asyncio.to_thread(_purge)
        if errs:
            logger.warning("study_delete_minio_partial", study_uid=study_uid, errors=errs)
        else:
            logger.info("study_deleted", study_uid=study_uid, artifacts_removed=deleted)
    except Exception as exc:
        logger.warning("study_delete_minio_failed", study_uid=study_uid, error=str(exc))


orthanc_router = APIRouter(prefix="/orthanc", tags=["orthanc"])


def _orthanc_label(value: str) -> str:
    # Orthanc labels allow [A-Za-z0-9_-] only.
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:64]


@orthanc_router.post("/notify-stable-study", status_code=202)
async def on_stable_study(
    body: OrthancStableStudyNotification,
    request: Request,
    background_tasks: BackgroundTasks,
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
    routing_service: Annotated[RoutingService, Depends(get_routing_service)],
):
    """Called by Orthanc's Lua ``OnStableStudy`` callback when a study becomes stable.

    Answers immediately and ingests in the background. Orthanc runs Lua callbacks while
    holding its Lua lock and ``HttpPost`` blocks until we reply, while ingest has to call
    back into Orthanc's REST API — which needs that same lock. Doing the ingest inline
    therefore deadlocked Orthanc.
    """
    import hmac

    import structlog as _sl

    settings = get_settings()
    if settings.orthanc_webhook_secret:
        presented = request.headers.get("X-Orthanc-Webhook-Secret", "")
        if not hmac.compare_digest(presented, settings.orthanc_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
    else:
        _sl.get_logger(__name__).warning("orthanc_webhook_secret_not_configured")

    background_tasks.add_task(
        ingest_stable_study, body.orthanc_id, body.study_instance_uid, registry, routing_service
    )
    return {"study_instance_uid": body.study_instance_uid, "status": "accepted"}


async def ingest_stable_study(
    orthanc_id: str,
    study_instance_uid: str,
    registry: UseCaseRegistry,
    routing_service: RoutingService,
) -> dict:
    """Attribute a stable PACS study to its tenant, ingest it and auto-route it.

    The PACS is shared by every tenant, so the tenant is resolved in order:

    1. a pending claim from an authenticated upload (API key or in-app upload);
    2. the tenant owning the study's *called* AE title (``tenant_dicom_endpoints``,
       read back from Orthanc's per-instance ``CalledAET``/``RemoteAET`` metadata);
    3. ``settings.dicom_unmapped_aet_tenant`` — "default" (Admin tenant) for
       single-tenant installs; empty in multi-tenant production, in which case the study
       is left unattributed (labelled ``unattributed`` in Orthanc, visible only to
       platform admins) instead of being filed under a tenant that never sent it.

    Resolution runs under a platform DB scope; once known, the scope is narrowed to the
    study's tenant so job creation is confined to it by Row-Level Security.
    """
    import structlog as _sl

    from app.application.dicom_endpoint_service import DicomEndpointService
    from app.infrastructure.database.session import async_session_factory
    from app.infrastructure.tenant.db_scope import platform_scope, tenant_scope

    logger = _sl.get_logger(__name__)
    settings = get_settings()
    pacs = OrthancPACSClient()
    try:
        with platform_scope():
            async with async_session_factory() as session:
                try:
                    origin: dict[str, str] = {}
                    try:
                        origin = await pacs.get_study_origin(orthanc_id)
                    except Exception as exc:
                        logger.warning("orthanc_study_origin_unavailable", orthanc_id=orthanc_id, error=str(exc))

                    ae_tenant = await DicomEndpointService(session).resolve_tenant(
                        origin.get("called_aet"), origin.get("calling_aet")
                    )
                    fallback = ae_tenant or settings.dicom_unmapped_aet_tenant or None

                    service = build_study_service(session, tenant_id=None)
                    try:
                        study = await service.ingest_study(study_instance_uid, fallback_tenant_id=fallback)
                    except StudyUnattributedError:
                        logger.warning(
                            "study_unattributed", study_uid=study_instance_uid,
                            called_aet=origin.get("called_aet"), calling_aet=origin.get("calling_aet"),
                        )
                        try:
                            await pacs.add_study_label(orthanc_id, "unattributed")
                        except Exception:
                            pass
                        return {"study_instance_uid": study_instance_uid, "status": "unattributed"}

                    try:
                        await pacs.add_study_label(orthanc_id, _orthanc_label(f"tenant_{study.tenant_id}"))
                    except Exception as exc:
                        logger.warning("orthanc_label_failed", orthanc_id=orthanc_id, error=str(exc))

                    allowed = None
                    if settings.multi_tenant_enabled:
                        from app.infrastructure.tenant.repository import get_tenant_by_id

                        entitled = await get_tenant_by_id(study.tenant_id)
                        allowed = set(entitled.features) if entitled else set()
                    with tenant_scope(study.tenant_id):
                        await reapply_scope(session)
                        orchestrator = build_job_orchestrator(
                            session, study.tenant_id, registry, routing_service, allowed
                        )
                        jobs = await orchestrator.create_jobs_for_study(study_instance_uid)
                        await session.commit()
                    logger.info(
                        "stable_study_ingested", study_uid=study_instance_uid,
                        tenant_id=study.tenant_id, jobs_created=len(jobs),
                    )
                    return {
                        "study_instance_uid": study_instance_uid,
                        "tenant_id": study.tenant_id,
                        "jobs_created": len(jobs),
                    }
                except Exception as exc:
                    await session.rollback()
                    logger.error("stable_study_ingest_failed", study_uid=study_instance_uid, error=str(exc))
                    return {"study_instance_uid": study_instance_uid, "status": "failed"}
    finally:
        await pacs.close()


@orthanc_router.get("/studies", dependencies=[require_permission("study.view")])
async def list_orthanc_studies(
    request: Request,
    service: Annotated[StudyService, Depends(get_study_service)],
):
    """List studies currently in Orthanc PACS.

    Orthanc is shared by every tenant, so a tenant user only sees the PACS studies
    already ingested into their own workspace; the full PACS listing (including
    not-yet-ingested studies, which belong to no tenant yet) is platform-admin only.
    """
    client = OrthancPACSClient()
    try:
        studies = await client.list_all_studies()
    finally:
        await client.close()
    if getattr(request.state, "is_platform_admin", False):
        return studies
    owned = await service.owned_study_uids([s["study_instance_uid"] for s in studies])
    return [s for s in studies if s["study_instance_uid"] in owned]


@orthanc_router.delete(
    "/studies/{orthanc_id}",
    status_code=204,
    dependencies=[require_permission("study.delete")],
)
async def delete_orthanc_study(
    orthanc_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[StudyService, Depends(get_study_service)],
):
    """Permanently delete a study from Orthanc PACS.

    ``orthanc_id`` is Orthanc's internal study ID (from GET /orthanc/studies), not a
    DICOM UID. This removes the underlying DICOM instances from the on-site PACS —
    irreversible unless the scanner resends the study. If the study was already
    ingested into the platform, its record and any AI results are left untouched, but
    the viewer will no longer be able to load images for it.
    """
    if not get_settings().orthanc_delete_enabled:
        raise HTTPException(status_code=403, detail="Orthanc study deletion is disabled")

    client = OrthancPACSClient()
    try:
        # The PACS is shared across tenants: only a study already ingested into the
        # caller's workspace may be deleted by a tenant user (platform admins may
        # remove any, including never-ingested studies).
        if not getattr(request.state, "is_platform_admin", False):
            study_uid = await client.get_study_instance_uid(orthanc_id)
            if not study_uid or not await service.owned_study_uids([study_uid]):
                raise HTTPException(status_code=404, detail=f"Orthanc study {orthanc_id} not found")
        await client.delete_study(orthanc_id)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Orthanc study {orthanc_id} not found")
        raise HTTPException(status_code=502, detail=f"Orthanc delete failed: {exc}")
    finally:
        await client.close()

    actor_id, actor_display, client_ip = actor_from_request(request)
    await AuditService(session).record(
        action="orthanc_study_deleted",
        entity_type="orthanc_study",
        entity_id=orthanc_id,
        actor_id=actor_id,
        actor_display=actor_display,
        client_ip=client_ip,
        commit=True,
    )


def _tat_minutes(start, end) -> float | None:
    if not start or not end:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def _to_response(study) -> StudyResponse:
    reading_status = getattr(study, "reading_status", "unread") or "unread"
    reported_at = getattr(study, "reported_at", None)
    signed_at = getattr(study, "signed_at", None)
    return StudyResponse(
        study_instance_uid=study.study_instance_uid,
        reading_status=reading_status,
        assigned_to=getattr(study, "assigned_to", None),
        assigned_to_username=getattr(study, "assigned_to_username", None),
        assigned_at=getattr(study, "assigned_at", None),
        reported_at=reported_at,
        signed_at=signed_at,
        tat_report_minutes=_tat_minutes(study.created_at, reported_at),
        tat_signoff_minutes=_tat_minutes(study.created_at, signed_at),
        patient_id=study.patient_id,
        patient_name=study.patient_name,
        patient_sex=study.patient_sex,
        patient_age=study.patient_age,
        patient_weight_kg=study.patient_weight_kg,
        patient_height_cm=study.patient_height_cm,
        study_date=study.study_date,
        study_description=study.study_description,
        accession_number=study.accession_number,
        referring_physician=study.referring_physician,
        body_part_examined=study.body_part_examined.value if study.body_part_examined else None,
        modality=study.modality,
        institution_name=study.institution_name,
        series=[
            SeriesResponse(
                series_instance_uid=s.series_instance_uid,
                series_number=s.series_number,
                series_description=s.series_description,
                modality=s.modality,
                body_part_examined=s.body_part_examined,
                protocol_name=getattr(s, "protocol_name", None),
                num_instances=s.num_instances,
                slice_thickness=s.slice_thickness,
            )
            for s in (study.series or [])
        ],
        created_at=study.created_at,
        updated_at=study.updated_at,
    )
