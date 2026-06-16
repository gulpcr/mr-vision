import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.routing_service import RoutingService
from app.application.usecase_registry import UseCaseRegistry
from app.interface.api.dependencies import get_registry, get_routing_service, get_session
from app.interface.schemas.usecase import (
    RoutingRulesResponse,
    UpdateRoutingRulesRequest,
    UseCaseListResponse,
    UseCaseResponse,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/routing-rules", response_model=RoutingRulesResponse)
async def get_routing_rules(
    routing_service: Annotated[RoutingService, Depends(get_routing_service)],
):
    rules = routing_service.get_all_rules()
    return RoutingRulesResponse(routing_rules=rules)


@router.put("/routing-rules")
async def update_routing_rules(
    body: UpdateRoutingRulesRequest,
    routing_service: Annotated[RoutingService, Depends(get_routing_service)],
):
    routing_service.update_site_rules(body.rules)
    return {"status": "ok", "message": "Routing rules updated"}


@router.get("/usecases", response_model=UseCaseListResponse)
async def admin_list_usecases(
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    usecases = registry.usecases
    return UseCaseListResponse(
        usecases=[
            UseCaseResponse(
                name=uc.name,
                version=uc.version,
                description=uc.description,
                supported_body_parts=uc.supported_body_parts,
                required_sequences=uc.required_sequences,
                model_type=uc.model_type,
                enabled=uc.enabled,
                module_path=uc.module_path,
                registered_at=uc.registered_at,
            )
            for uc in usecases.values()
        ]
    )


@router.get("/usecases/{usecase_name}/manifest")
async def get_usecase_manifest(
    usecase_name: str,
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    manifest = registry.get_manifest(usecase_name)
    if not manifest:
        from fastapi import HTTPException
        raise HTTPException(404, f"Manifest not found for {usecase_name}")
    return manifest


@router.get("/site-config")
async def get_site_config():
    from app.config import get_settings
    import yaml

    settings = get_settings()
    path = settings.site_config_path
    if not path.exists():
        return {"site_id": settings.site_id, "config": {}}
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    return {"site_id": settings.site_id, "config": config}


class UpdateSiteConfigRequest(BaseModel):
    site_name: str | None = None
    dicom_ae_title: str | None = None
    routing_overrides: list[dict[str, Any]] = []


@router.put("/site-config")
async def update_site_config(body: UpdateSiteConfigRequest):
    from app.config import get_settings
    import yaml

    settings = get_settings()
    config = {
        "site_id": settings.site_id,
        "site_name": body.site_name or settings.site_id,
        "dicom_ae_title": body.dicom_ae_title or "MRI_AI_PLATFORM",
        "routing_overrides": body.routing_overrides,
    }
    path = settings.site_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    return {"status": "ok", "message": "Site configuration updated"}


# ── Audit Dashboard (F13) ──────────────────────────────────────

@router.get("/audit")
async def list_audit_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    action: str | None = None,
    entity_type: str | None = None,
    actor: str | None = None,
):
    """Paginated audit log with filters."""
    from app.infrastructure.database.models import AuditLogRecord
    from sqlalchemy import select, func

    stmt = select(AuditLogRecord).order_by(AuditLogRecord.timestamp.desc())
    count_stmt = select(func.count()).select_from(AuditLogRecord)

    if action:
        stmt = stmt.where(AuditLogRecord.action == action)
        count_stmt = count_stmt.where(AuditLogRecord.action == action)
    if entity_type:
        stmt = stmt.where(AuditLogRecord.entity_type == entity_type)
        count_stmt = count_stmt.where(AuditLogRecord.entity_type == entity_type)
    if actor:
        stmt = stmt.where(AuditLogRecord.actor == actor)
        count_stmt = count_stmt.where(AuditLogRecord.actor == actor)

    stmt = stmt.offset(offset).limit(limit)

    result = await session.execute(stmt)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    entries = [
        {
            "id": r.id,
            "action": r.action,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "actor": r.actor,
            "details": r.details,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        }
        for r in result.scalars().all()
    ]
    return {"entries": entries, "total": total, "offset": offset, "limit": limit}


# ── Retention Policies (F15) ───────────────────────────────────

@router.get("/retention")
async def list_retention_policies(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.retention_service import RetentionService
    service = RetentionService(session)
    return {"policies": await service.list_policies()}


@router.post("/retention", status_code=201)
async def create_retention_policy(
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.retention_service import RetentionService
    service = RetentionService(session)
    policy = await service.create_policy(
        name=body["name"],
        entity_type=body["entity_type"],
        max_age_days=body.get("max_age_days", 365),
        action=body.get("action", "archive"),
    )
    return policy


@router.delete("/retention/{policy_id}")
async def delete_retention_policy(
    policy_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.retention_service import RetentionService
    service = RetentionService(session)
    deleted = await service.delete_policy(policy_id)
    if not deleted:
        raise HTTPException(404, "Policy not found")
    return {"status": "ok"}


@router.post("/retention/apply")
async def apply_retention_policies(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.retention_service import RetentionService
    service = RetentionService(session)
    totals = await service.apply_policies()
    return {"status": "ok", "purged": totals}


# ── A/B Testing (F6) ──────────────────────────────────────────

@router.get("/experiments")
async def list_experiments(
    session: Annotated[AsyncSession, Depends(get_session)],
    usecase_name: str | None = None,
):
    from app.application.ab_testing_service import ABTestingService
    service = ABTestingService(session)
    return {"experiments": await service.list_experiments(usecase_name)}


@router.post("/experiments", status_code=201)
async def create_experiment(
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.ab_testing_service import ABTestingService
    service = ABTestingService(session)
    exp = await service.create_experiment(
        name=body["name"],
        usecase_name=body["usecase_name"],
        control_version=body["control_version"],
        treatment_version=body["treatment_version"],
        traffic_split=body.get("traffic_split", 0.5),
    )
    return exp


@router.get("/experiments/{experiment_id}/stats")
async def get_experiment_stats(
    experiment_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.ab_testing_service import ABTestingService
    service = ABTestingService(session)
    return await service.get_experiment_stats(experiment_id)


@router.post("/experiments/{experiment_id}/stop")
async def stop_experiment(
    experiment_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.ab_testing_service import ABTestingService
    service = ABTestingService(session)
    await service.stop_experiment(experiment_id)
    return {"status": "ok"}


# ── Alerting (F14) ─────────────────────────────────────────────

@router.get("/alerts")
async def list_alert_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.alerting_service import AlertingService
    service = AlertingService(session)
    return {"rules": await service.list_rules()}


@router.post("/alerts", status_code=201)
async def create_alert_rule(
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.alerting_service import AlertingService
    service = AlertingService(session)
    rule = await service.create_rule(
        name=body["name"],
        event_type=body["event_type"],
        webhook_url=body["webhook_url"],
        condition=body.get("condition"),
    )
    return rule


@router.delete("/alerts/{rule_id}")
async def delete_alert_rule(
    rule_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.alerting_service import AlertingService
    service = AlertingService(session)
    deleted = await service.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(404, "Alert rule not found")
    return {"status": "ok"}


@router.get("/alerts/history")
async def list_alert_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    rule_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    from app.application.alerting_service import AlertingService
    service = AlertingService(session)
    return {"history": await service.get_history(rule_id, limit)}


# ── Model Registry (F7) ───────────────────────────────────────

@router.get("/models/{usecase_name}/versions")
async def list_model_versions(
    usecase_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.model_registry import ModelRegistryService
    service = ModelRegistryService(session)
    return {"versions": await service.list_versions(usecase_name)}


@router.post("/models/{usecase_name}/versions", status_code=201)
async def register_model_version(
    usecase_name: str,
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.model_registry import ModelRegistryService
    service = ModelRegistryService(session)
    version = await service.register_version(
        usecase_name=usecase_name,
        version=body["version"],
        storage_path=body["storage_path"],
        checksum=body["checksum"],
        metadata=body.get("metadata"),
    )
    return version


@router.post("/models/{usecase_name}/versions/{version}/activate")
async def activate_model_version(
    usecase_name: str,
    version: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.model_registry import ModelRegistryService
    service = ModelRegistryService(session)
    activated = await service.activate_version(usecase_name, version)
    if not activated:
        raise HTTPException(404, "Model version not found")
    return {"status": "ok", "usecase": usecase_name, "version": version}


@router.get("/models/{usecase_name}/active")
async def get_active_model_version(
    usecase_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.model_registry import ModelRegistryService
    service = ModelRegistryService(session)
    version = await service.get_active_version(usecase_name)
    if not version:
        raise HTTPException(404, "No active model version")
    return version


# ── Review Queue (F20) ────────────────────────────────────────

@router.get("/review")
async def list_review_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
    status: str | None = None,
    usecase_name: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    from app.application.active_learning_service import ActiveLearningService
    service = ActiveLearningService(session)
    items = await service.list_review_queue(status, usecase_name, offset, limit)
    stats = await service.get_queue_stats()
    return {"items": items, "stats": stats, "offset": offset, "limit": limit}


@router.get("/review/stats")
async def get_review_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.active_learning_service import ActiveLearningService
    service = ActiveLearningService(session)
    return await service.get_queue_stats()


@router.get("/review/{review_id}")
async def get_review_item(
    review_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.active_learning_service import ActiveLearningService
    service = ActiveLearningService(session)
    item = await service.get_review_item(review_id)
    if not item:
        raise HTTPException(404, "Review item not found")
    return item


@router.post("/review/{review_id}/submit")
async def submit_review(
    review_id: str,
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
):
    from app.application.active_learning_service import ActiveLearningService
    service = ActiveLearningService(session)
    reviewer = getattr(request.state, "user", "unknown")
    item = await service.submit_review(
        review_id=review_id,
        status=body["status"],
        reviewer=reviewer,
        notes=body.get("notes", ""),
    )
    if not item:
        raise HTTPException(404, "Review item not found")
    return item


# ── Batch Upload (F19) ────────────────────────────────────────

@router.get("/batches")
async def list_batches(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.batch_service import BatchUploadService
    service = BatchUploadService(session)
    return {"batches": await service.list_batches()}


@router.post("/batches", status_code=201)
async def create_batch(
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
):
    from app.application.batch_service import BatchUploadService
    service = BatchUploadService(session)
    creator = getattr(request.state, "user", "unknown")
    batch = await service.create_batch(
        name=body["name"],
        study_uids=body["study_uids"],
        created_by=creator,
    )
    return batch


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.application.batch_service import BatchUploadService
    service = BatchUploadService(session)
    batch = await service.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return batch


# ── QA / Audit Metrics Dashboard (Feature 8) ─────────────────────────────────

@router.get("/metrics")
async def get_qa_metrics(
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int = Query(30, ge=1, le=365),
    usecase_name: str | None = None,
):
    """QA and audit metrics: TAT, agreement rates, QA flag rates."""
    from app.application.analytics_service import AnalyticsService

    service = AnalyticsService(session)
    return await service.get_qa_metrics(days=days, usecase_name=usecase_name)


# ── Capacity Prediction (Feature 11) ─────────────────────────────────────────

@router.get("/capacity")
async def get_capacity_metrics(
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int = Query(30, ge=1, le=365),
):
    """Scanner utilization analytics and 7-day demand forecast."""
    from app.application.analytics_service import AnalyticsService

    service = AnalyticsService(session)
    return await service.get_capacity_metrics(days=days)


# ── Longitudinal Trend (Feature 7) ───────────────────────────────────────────

@router.get("/patients/{patient_id}/trend/{usecase_name}")
async def get_patient_trend(
    patient_id: str,
    usecase_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Return all result timepoints for longitudinal trend chart."""
    from app.application.analytics_service import AnalyticsService

    service = AnalyticsService(session)
    return await service.get_patient_trend(patient_id=patient_id, usecase_name=usecase_name)


# ── Protocol Optimization (Feature 10) ───────────────────────────────────────

@router.get("/studies/{study_uid}/protocol-check")
async def check_protocol(
    study_uid: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Validate study series against registered use-case protocol requirements."""
    from app.interface.api.dependencies import get_registry

    from app.infrastructure.database.models import SeriesRecord
    from sqlalchemy import select

    stmt = select(SeriesRecord).where(SeriesRecord.study_instance_uid == study_uid)
    result = await session.execute(stmt)
    series = result.scalars().all()

    if not series:
        return {"study_instance_uid": study_uid, "issues": [], "status": "no_series"}

    issues = []
    for s in series:
        tags = s.dicom_tags or {}
        slice_thickness = s.slice_thickness

        # Check slice thickness
        if slice_thickness is not None:
            if slice_thickness > 5.0:
                issues.append({
                    "series_uid": s.series_instance_uid,
                    "series_description": s.series_description or "—",
                    "severity": "warning",
                    "code": "slice_thickness_too_large",
                    "message": f"Slice thickness {slice_thickness:.1f}mm exceeds 5.0mm — may degrade 3D segmentation accuracy.",
                    "suggestion": "Reacquire at ≤3mm slice thickness for optimal AI analysis.",
                })

        # Check for missing RepetitionTime (indicates incomplete DICOM tags)
        tr = tags.get("RepetitionTime")
        if tr is None:
            issues.append({
                "series_uid": s.series_instance_uid,
                "series_description": s.series_description or "—",
                "severity": "info",
                "code": "missing_tr",
                "message": "RepetitionTime not stored in DICOM tags — cannot validate sequence parameters.",
                "suggestion": "Verify scanner exports TR/TE values in DICOM headers.",
            })

        # Check for extremely short TR (possibly wrong sequence type)
        if tr is not None:
            try:
                tr_val = float(tr)
                if tr_val < 100:
                    issues.append({
                        "series_uid": s.series_instance_uid,
                        "series_description": s.series_description or "—",
                        "severity": "warning",
                        "code": "tr_too_short",
                        "message": f"RepetitionTime {tr_val:.0f}ms is unusually short for MRI — possible sequence mismatch.",
                        "suggestion": "Verify this is the intended MRI sequence.",
                    })
            except (TypeError, ValueError):
                pass

    status = "ok" if not issues else "warnings" if all(i["severity"] != "error" for i in issues) else "errors"
    return {
        "study_instance_uid": study_uid,
        "series_checked": len(series),
        "issues": issues,
        "status": status,
    }


# ── Prior Auto-Comparison (Feature 5) ────────────────────────────────────────

@router.get("/studies/{study_uid}/prior-comparison/{usecase_name}")
async def get_prior_comparison(
    study_uid: str,
    usecase_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Find the most recent prior result for the same patient+usecase and compare."""
    from app.infrastructure.database.models import ResultRecord, StudyRecord
    from sqlalchemy import select

    # Get current study to find patient_id
    study_stmt = select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
    study_res = await session.execute(study_stmt)
    study = study_res.scalar_one_or_none()
    if not study:
        raise HTTPException(404, "Study not found")
    if not study.patient_id:
        return {"status": "no_patient_id", "comparison": None}

    # Get current result
    cur_stmt = select(ResultRecord).where(
        ResultRecord.study_instance_uid == study_uid,
        ResultRecord.usecase_name == usecase_name,
        ResultRecord.is_latest == True,
    )
    cur_res = await session.execute(cur_stmt)
    current_result = cur_res.scalar_one_or_none()
    if not current_result:
        return {"status": "no_current_result", "comparison": None}

    # Find most recent prior result for same patient + usecase
    prior_stmt = (
        select(ResultRecord)
        .join(StudyRecord, ResultRecord.study_instance_uid == StudyRecord.study_instance_uid)
        .where(
            StudyRecord.patient_id == study.patient_id,
            ResultRecord.usecase_name == usecase_name,
            ResultRecord.is_latest == True,
            ResultRecord.id != current_result.id,
        )
        .order_by(ResultRecord.created_at.desc())
        .limit(1)
    )
    prior_res = await session.execute(prior_stmt)
    prior_result = prior_res.scalar_one_or_none()

    if not prior_result:
        raise HTTPException(404, "No prior study found for this patient and use case")

    from app.application.result_service import ResultService
    from app.interface.api.dependencies import get_artifact_store
    from app.infrastructure.database.repositories import PgResultRepository

    result_service = ResultService(
        result_repo=PgResultRepository(session),
        artifact_store=get_artifact_store(),
    )
    data = await result_service.compare_results(prior_result.id, current_result.id)

    from app.interface.schemas.result import CompareResponse, DeltaResponse, MeasurementDelta
    from app.interface.api.results import _to_response

    ra = _to_response(data["result_a"])
    rb = _to_response(data["result_b"])
    d = data["delta"]

    return CompareResponse(
        usecase_name=rb.usecase_name,
        result_a=ra,
        result_b=rb,
        delta=DeltaResponse(
            measurements={k: MeasurementDelta(**v) for k, v in d["measurements"].items()},
            qa_flags_new=d["qa_flags_new"],
            qa_flags_resolved=d["qa_flags_resolved"],
            days_between=d["days_between"],
        ),
    )


# ── Worklist Urgency Scores (Feature 3) ──────────────────────────────────────

@router.post("/urgency-scores")
async def compute_urgency_scores(
    body: dict,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Compute AI-based urgency scores for a list of study UIDs."""
    study_uids = body.get("study_uids", [])
    if not study_uids:
        return {"scores": []}

    from app.application.analytics_service import AnalyticsService

    service = AnalyticsService(session)
    scores = await service.compute_urgency_scores(study_uids)
    return {"scores": scores}


# ── Data Reset (Danger Zone) ──────────────────────────────────────────────────

@router.post("/reset")
async def reset_all_data(
    session: Annotated[AsyncSession, Depends(get_session)],
    confirm: bool = False,
):
    """DESTRUCTIVE — wipe all clinical data and return to a clean state.

    Clears:
      - studies, series (cascade: job_runs, results_index)
      - critical_alerts, review_queue, audit_log, share_links
      - All MinIO artifacts

    Preserved (not touched):
      - users, tenants, routing rules, alert rules, retention policies,
        model_versions, ab_experiments, batch_uploads, usecase_registry

    Must pass ?confirm=true or the request is rejected.
    """
    if not confirm:
        raise HTTPException(
            400,
            "Safety check: add ?confirm=true to execute this destructive operation.",
        )

    import structlog as _structlog
    logger = _structlog.get_logger(__name__)

    from app.infrastructure.database.models import (
        AuditLogRecord,
        CriticalAlertRecord,
        ReviewQueueRecord,
        ShareLinkRecord,
        StudyRecord,
    )

    totals: dict[str, int] = {}

    # ── 1. Tables with no FK dependency on studies ──────────────────
    r = await session.execute(delete(CriticalAlertRecord))
    totals["critical_alerts"] = r.rowcount

    r = await session.execute(delete(ReviewQueueRecord))
    totals["review_queue"] = r.rowcount

    r = await session.execute(delete(AuditLogRecord))
    totals["audit_log"] = r.rowcount

    r = await session.execute(delete(ShareLinkRecord))
    totals["share_links"] = r.rowcount

    # ── 2. Studies — cascades to series, job_runs, results_index ────
    r = await session.execute(delete(StudyRecord))
    totals["studies"] = r.rowcount

    await session.commit()

    # ── 3. MinIO — remove every object in the artifact bucket ───────
    totals["artifacts_deleted"] = 0
    totals["artifact_errors"] = 0
    try:
        from app.infrastructure.storage.client import get_artifact_store

        store = get_artifact_store()

        def _bulk_delete() -> tuple[int, int]:
            from minio.deleteobjects import DeleteObject

            objects = list(store._client.list_objects(store._bucket, recursive=True))
            if not objects:
                return 0, 0
            delete_gen = (DeleteObject(o.object_name) for o in objects)
            errors = list(store._client.remove_objects(store._bucket, delete_gen))
            return len(objects), len(errors)

        deleted, errs = await asyncio.to_thread(_bulk_delete)
        totals["artifacts_deleted"] = deleted - errs
        totals["artifact_errors"] = errs
    except Exception as exc:
        logger.warning("minio_reset_failed", error=str(exc))
        totals["artifact_errors"] = -1  # sentinel: MinIO itself unreachable

    logger.info("data_reset_completed", totals=totals)
    return {"status": "ok", "cleared": totals}
