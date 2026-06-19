import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.study_service import StudyService
from app.infrastructure.orthanc.client import OrthancPACSClient
from app.application.job_orchestrator import JobOrchestrator
from app.application.routing_service import RoutingService
from app.interface.api.dependencies import (
    get_study_service,
    get_job_orchestrator,
    get_routing_service,
    get_session,
)
from app.interface.api.validators import validate_dicom_uid
from app.interface.schemas.study import (
    OrthancStableStudyNotification,
    SeriesResponse,
    StudyIngestRequest,
    StudyListResponse,
    StudyResponse,
)

router = APIRouter(prefix="/studies", tags=["studies"])


@router.get("", response_model=StudyListResponse)
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


@router.get("/{study_uid}", response_model=StudyResponse)
async def get_study(
    study_uid: str,
    service: Annotated[StudyService, Depends(get_study_service)],
):
    validate_dicom_uid(study_uid)
    study = await service.get_study(study_uid)
    if not study:
        raise HTTPException(status_code=404, detail=f"Study {study_uid} not found")
    return _to_response(study)


@router.get("/{study_uid}/routing-preview")
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


@router.post("", response_model=StudyResponse, status_code=201)
async def ingest_study(
    body: StudyIngestRequest,
    service: Annotated[StudyService, Depends(get_study_service)],
):
    try:
        study = await service.ingest_study(body.study_instance_uid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_response(study)


@router.delete("/{study_uid}", status_code=204)
async def delete_study(
    study_uid: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Remove a study and all related data from the platform.

    Cascades through the DB (series, job_runs, results_index) via FK constraints.
    Also deletes every MinIO artifact stored under the study prefix.
    Orthanc PACS data is NOT touched — the DICOM files remain in the viewer.
    """
    import structlog as _sl
    from app.infrastructure.database.models import StudyRecord

    logger = _sl.get_logger(__name__)

    result = await session.execute(
        delete(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Study {study_uid} not found")
    await session.commit()

    # Remove MinIO artifacts for this study (non-fatal — log warning on failure)
    try:
        from app.infrastructure.storage.client import get_artifact_store
        from minio.deleteobjects import DeleteObject

        store = get_artifact_store()

        def _purge():
            objects = list(store._client.list_objects(
                store._bucket, prefix=f"{study_uid}/", recursive=True
            ))
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


@orthanc_router.post("/notify-stable-study")
async def on_stable_study(
    body: OrthancStableStudyNotification,
    service: Annotated[StudyService, Depends(get_study_service)],
    orchestrator: Annotated[JobOrchestrator, Depends(get_job_orchestrator)],
):
    """Called by Orthanc Lua script when a study becomes stable.

    Ingests the study metadata and auto-routes to applicable use cases.
    """
    study = await service.ingest_study(body.study_instance_uid)
    jobs = await orchestrator.create_jobs_for_study(body.study_instance_uid)
    return {
        "study_instance_uid": body.study_instance_uid,
        "orthanc_id": body.orthanc_id,
        "jobs_created": len(jobs),
        "job_ids": [j.id for j in jobs],
    }


@orthanc_router.get("/studies")
async def list_orthanc_studies():
    """List all studies currently in Orthanc PACS.
    Use the study_instance_uid from the results to ingest via POST /api/studies."""
    client = OrthancPACSClient()
    try:
        return await client.list_all_studies()
    finally:
        await client.close()


def _to_response(study) -> StudyResponse:
    return StudyResponse(
        study_instance_uid=study.study_instance_uid,
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
