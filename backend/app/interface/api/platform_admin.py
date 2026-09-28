"""Platform operator console (platform admins only — cross-tenant by design).

GET  /admin/platform/overview                   usage per tenant (studies, users, jobs, storage)
GET  /admin/platform/audit                      audit log across tenants (filterable)
GET  /admin/platform/users                      cross-tenant user search (metadata only)
POST /admin/platform/users/{id}/revoke-sessions sign a user out everywhere
POST /admin/platform/users/{id}/reset-link      one-time password-set link (INVITATION_TTL_HOURS)
PUT  /admin/platform/tenants/{id}/limits        seat limit
POST /admin/platform/tenants/{id}/purge         irreversibly delete a tenant's data

Every route depends on require_platform_admin, which also widens the DB scope to the
platform (RLS sees every tenant). Mutations are audited in the affected tenant's log.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth_service import AuthService
from app.domain.models import AuditEntry
from app.infrastructure.database.models import (
    AuditLogRecord,
    JobRunRecord,
    StudyRecord,
    TenantRecord,
    UserRecord,
)
from app.infrastructure.database.repositories import PgAuditRepository
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_platform_admin

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/admin/platform", tags=["platform"], dependencies=[Depends(require_platform_admin)],
)

# Tenant-owned tables purged by /purge, children first. audit_log is kept: the purge
# itself must stay on record and the audit hash chain must not be broken.
_PURGE_TABLES = [
    "dashboard_versions", "dashboard_layouts", "tenant_dicom_endpoints",
    "critical_alerts", "review_queue", "share_links", "mammography_reports", "mri_reports",
    "observations", "conditions", "orders", "patients", "batch_upload_items", "batch_uploads",
    "alert_history", "alert_rules", "retention_policies", "pending_study_tenants",
    "studies", "user_roles", "tenant_api_keys", "roles", "users",
    "tenant_settings", "tenant_branding",
]


class LimitsUpdate(BaseModel):
    max_users: int | None = Field(default=None, ge=1, le=100_000)


async def _count_by_tenant(session, stmt) -> dict[str, int]:
    return {tid: int(n) for tid, n in (await session.execute(stmt)).all()}


@router.get("/overview")
async def platform_overview(session: Annotated[AsyncSession, Depends(get_session)]):
    """One row per tenant: users, studies (total / last 30 days), AI jobs failed in the
    last 7 days, artifact storage, last study received."""
    now = datetime.now(timezone.utc)
    month, week = now - timedelta(days=30), now - timedelta(days=7)

    tenants = (await session.execute(select(TenantRecord).order_by(TenantRecord.name))).scalars().all()
    users = await _count_by_tenant(session, select(UserRecord.tenant_id, func.count()).group_by(UserRecord.tenant_id))
    studies = await _count_by_tenant(session, select(StudyRecord.tenant_id, func.count()).group_by(StudyRecord.tenant_id))
    studies_30d = await _count_by_tenant(session, select(StudyRecord.tenant_id, func.count())
                                         .where(StudyRecord.created_at >= month).group_by(StudyRecord.tenant_id))
    failed_7d = await _count_by_tenant(session, select(JobRunRecord.tenant_id, func.count())
                                       .where(JobRunRecord.status == "failed", JobRunRecord.created_at >= week)
                                       .group_by(JobRunRecord.tenant_id))
    active_jobs = await _count_by_tenant(session, select(JobRunRecord.tenant_id, func.count())
                                         .where(JobRunRecord.status.in_(("pending", "preprocessing", "inferring", "postprocessing")))
                                         .group_by(JobRunRecord.tenant_id))
    last_study = {tid: ts for tid, ts in (await session.execute(
        select(StudyRecord.tenant_id, func.max(StudyRecord.created_at)).group_by(StudyRecord.tenant_id)
    )).all()}
    storage = {tid: int(b or 0) for tid, b in (await session.execute(text(
        "SELECT r.tenant_id, SUM(COALESCE((a->>'size_bytes')::bigint, 0)) "
        "FROM results_index r, json_array_elements(COALESCE(r.artifacts, '[]'::json)) a "
        "GROUP BY r.tenant_id"
    ))).all()}

    rows = [{
        "tenant_id": t.id, "name": t.name, "slug": t.slug, "status": t.status, "plan": t.plan,
        "max_users": t.max_users, "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
        "users": users.get(t.id, 0), "studies": studies.get(t.id, 0),
        "studies_30d": studies_30d.get(t.id, 0), "jobs_failed_7d": failed_7d.get(t.id, 0),
        "jobs_active": active_jobs.get(t.id, 0), "storage_bytes": storage.get(t.id, 0),
        "last_study_at": last_study[t.id].isoformat() if last_study.get(t.id) else None,
    } for t in tenants]
    totals = {k: sum(r[k] for r in rows) for k in
              ("users", "studies", "studies_30d", "jobs_failed_7d", "jobs_active", "storage_bytes")}
    return {"tenants": rows, "totals": {**totals, "tenants": len(rows)}}


@router.get("/audit")
async def platform_audit(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    stmt = select(AuditLogRecord).order_by(AuditLogRecord.timestamp.desc())
    if tenant_id:
        stmt = stmt.where(AuditLogRecord.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditLogRecord.action == action)
    if actor:
        stmt = stmt.where(or_(AuditLogRecord.actor == actor, AuditLogRecord.actor_display == actor))
    rows = (await session.execute(stmt.offset(offset).limit(limit))).scalars().all()
    return [{
        "id": r.id, "tenant_id": r.tenant_id, "action": r.action, "entity_type": r.entity_type,
        "entity_id": r.entity_id, "actor": r.actor_display or r.actor, "outcome": r.outcome,
        "client_ip": r.client_ip, "details": r.details or {}, "seq": r.seq,
        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
    } for r in rows]


@router.get("/users")
async def search_users(
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str = Query("", max_length=128),
    tenant_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
):
    stmt = select(UserRecord).order_by(UserRecord.tenant_id, UserRecord.username).limit(limit)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(UserRecord.username).like(like),
                              func.lower(UserRecord.email).like(like),
                              func.lower(UserRecord.full_name).like(like)))
    if tenant_id:
        stmt = stmt.where(UserRecord.tenant_id == tenant_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [{
        "id": u.id, "username": u.username, "email": u.email, "full_name": u.full_name,
        "tenant_id": u.tenant_id, "role": u.role, "is_active": u.is_active, "status": u.status,
        "totp_enabled": u.totp_enabled, "is_platform_admin": u.is_platform_admin,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in rows]


async def _user_or_404(session, user_id: str) -> UserRecord:
    user = (await session.execute(select(UserRecord).where(UserRecord.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: str, actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.infrastructure.auth.principal import invalidate_principal

    user = await _user_or_404(session, user_id)
    await AuthService(session).revoke_sessions(user.id, user.tenant_id, actor=f"platform:{actor}")
    invalidate_principal(user.id)
    return {"status": "ok"}


@router.post("/users/{user_id}/reset-link")
async def reset_link(
    user_id: str, request: Request, actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.interface.api.auth import _invite_link

    user = await _user_or_404(session, user_id)
    token = await AuthService(session).issue_password_reset(user.id, user.tenant_id, actor=f"platform:{actor}")
    from app.interface.api.auth import _ttl_hours

    return {"reset_link": await _invite_link(request, token, user.tenant_id),
            "expires_in_hours": _ttl_hours()}


@router.put("/tenants/{tenant_id}/limits")
async def update_limits(
    tenant_id: str, body: LimitsUpdate, actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    tenant = await session.get(TenantRecord, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.max_users = body.max_users
    await PgAuditRepository(session, tenant_id=tenant_id).save(AuditEntry(
        action="config_changed", entity_type="tenant", entity_id=tenant_id, actor=f"platform:{actor}",
        details={"max_users": body.max_users}, tenant_id=tenant_id,
    ))
    return {"tenant_id": tenant_id, "max_users": tenant.max_users}


@router.post("/tenants/{tenant_id}/purge")
async def purge_tenant(
    tenant_id: str, actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    confirm: str = Query(..., description="Must equal the tenant's slug"),
):
    """IRREVERSIBLE: delete every row the tenant owns (except its audit log), its MinIO
    artifacts and its Orthanc studies, then mark the tenant offboarded + deleted.
    The platform (Admin) tenant cannot be purged."""
    from app.infrastructure.auth.principal import PLATFORM_TENANT_ID

    tenant = await session.get(TenantRecord, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if tenant_id == PLATFORM_TENANT_ID:
        raise HTTPException(status_code=409, detail="The platform tenant cannot be purged")
    if confirm != tenant.slug:
        raise HTTPException(status_code=400, detail="Confirmation does not match the tenant slug")

    study_uids = [r[0] for r in (await session.execute(
        select(StudyRecord.study_instance_uid).where(StudyRecord.tenant_id == tenant_id)
    )).all()]
    counts: dict[str, int] = {}
    for table in _PURGE_TABLES:
        result = await session.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        counts[table] = result.rowcount or 0
    tenant.status = "offboarded"
    tenant.is_active = False
    tenant.deleted_at = datetime.now(timezone.utc)
    await PgAuditRepository(session, tenant_id=tenant_id).save(AuditEntry(
        action="tenant_purged", entity_type="tenant", entity_id=tenant_id, actor=f"platform:{actor}",
        details={"rows_deleted": counts, "studies": len(study_uids)}, tenant_id=tenant_id,
    ))
    await session.commit()

    artifacts_deleted = await asyncio.to_thread(_purge_artifacts, tenant_id, study_uids)
    orthanc_deleted = await _purge_orthanc(tenant_id, study_uids)
    logger.warning("tenant_purged", tenant_id=tenant_id, studies=len(study_uids),
                   artifacts=artifacts_deleted, orthanc=orthanc_deleted)
    return {"tenant_id": tenant_id, "rows_deleted": counts, "artifacts_deleted": artifacts_deleted,
            "orthanc_studies_deleted": orthanc_deleted}


def _purge_artifacts(tenant_id: str, study_uids: list[str]) -> int:
    try:
        from minio.deleteobjects import DeleteObject

        from app.domain.storage_keys import study_prefixes
        from app.infrastructure.storage.client import get_artifact_store

        store = get_artifact_store()
        objects = list(store._client.list_objects(store._bucket, prefix=f"{tenant_id}/", recursive=True))
        for uid in study_uids:  # pre-tenancy legacy keys
            objects.extend(store._client.list_objects(
                store._bucket, prefix=study_prefixes(None, uid)[-1], recursive=True))
        if objects:
            list(store._client.remove_objects(store._bucket, (DeleteObject(o.object_name) for o in objects)))
        return len(objects)
    except Exception as exc:
        logger.warning("tenant_purge_minio_failed", tenant_id=tenant_id, error=str(exc))
        return -1


async def _purge_orthanc(tenant_id: str, study_uids: list[str]) -> int:
    from app.infrastructure.orthanc.client import OrthancPACSClient

    client = OrthancPACSClient()
    deleted = 0
    try:
        for uid in study_uids:
            try:
                await client.delete_study(await client.get_orthanc_study_id(uid))
                deleted += 1
            except Exception:
                continue
    finally:
        await client.close()
    return deleted
