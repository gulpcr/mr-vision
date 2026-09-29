"""Server-side widget data for dashboards (registry keyed by widget type).

Every handler runs SQL aggregates through the request's session, so Row-Level Security
confines it to the caller's tenant (and, for the referring Doctor, to their referred
studies). The handlers additionally filter by tenant — and by referral where it applies —
explicitly, so the numbers stay correct when RLS is not enforced (owner connection).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, select

from app.infrastructure.database.models import (
    CriticalAlertRecord,
    JobRunRecord,
    OrderRecord,
    PatientRecord,
    ResultRecord,
    StudyRecord,
)
from app.config import get_settings
from app.domain.patient_identity import displayed_patient_name

_PERIOD_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
_OPEN_ALERT_STATUSES = ("pending", "escalated", "notified")


@dataclass(frozen=True)
class WidgetContext:
    tenant_id: str
    user_id: str
    referring_user_id: str | None  # set for referral-scoped users (the referring Doctor)


def _since(config: dict[str, Any]) -> tuple[datetime, int]:
    days = _PERIOD_DAYS.get(str(config.get("period", "30d")), 30)
    return datetime.now(timezone.utc) - timedelta(days=days), days


def _limit(config: dict[str, Any], default: int = 8) -> int:
    try:
        return max(1, min(50, int(config.get("limit", default))))
    except (TypeError, ValueError):
        return default


def _studies(ctx: WidgetContext, *criteria):
    conds = [StudyRecord.tenant_id == ctx.tenant_id, *criteria]
    if ctx.referring_user_id:
        conds.append(StudyRecord.referring_user_id == ctx.referring_user_id)
    return and_(*conds)


def _study_uid_scope(ctx: WidgetContext, uid_column):
    """Restrict a study-derived table to the referral-scoped user's studies."""
    if not ctx.referring_user_id:
        return True
    return uid_column.in_(
        select(StudyRecord.study_instance_uid).where(
            StudyRecord.tenant_id == ctx.tenant_id,
            StudyRecord.referring_user_id == ctx.referring_user_id,
        )
    )


def _study_row(r: StudyRecord) -> dict[str, Any]:
    return {
        "study_instance_uid": r.study_instance_uid,
        "patient_name": displayed_patient_name(
            r.patient_name, r.patient_id, get_settings().display_patient_names
        ),
        "patient_id": r.patient_id,
        "modality": r.modality,
        "description": r.study_description,
        "reading_status": r.reading_status,
        "assigned_to": r.assigned_to_username,
        "received_at": r.created_at.isoformat() if r.created_at else None,
        "reported_at": r.reported_at.isoformat() if r.reported_at else None,
        "signed_at": r.signed_at.isoformat() if r.signed_at else None,
    }


async def _kpi_studies(session, ctx, config):
    since, days = _since(config)
    prev_since = since - timedelta(days=days)
    extra = [StudyRecord.modality == config["modality"].upper()] if config.get("modality") else []
    current = (await session.execute(
        select(func.count()).select_from(StudyRecord).where(_studies(ctx, StudyRecord.created_at >= since, *extra))
    )).scalar_one()
    previous = (await session.execute(
        select(func.count()).select_from(StudyRecord).where(
            _studies(ctx, StudyRecord.created_at >= prev_since, StudyRecord.created_at < since, *extra)
        )
    )).scalar_one()
    return {"value": current, "previous": previous, "delta": current - previous}


async def _reading_status(session, ctx, config):
    since, _ = _since(config)
    rows = (await session.execute(
        select(StudyRecord.reading_status, func.count())
        .where(_studies(ctx, StudyRecord.created_at >= since))
        .group_by(StudyRecord.reading_status)
    )).all()
    counts = {status: 0 for status in ("unread", "in_progress", "reported", "signed")}
    counts.update({s or "unread": n for s, n in rows})
    return {"series": [{"label": k, "value": v} for k, v in counts.items()]}


async def _studies_by_modality(session, ctx, config):
    since, _ = _since(config)
    rows = (await session.execute(
        select(StudyRecord.modality, func.count())
        .where(_studies(ctx, StudyRecord.created_at >= since))
        .group_by(StudyRecord.modality)
        .order_by(func.count().desc())
    )).all()
    return {"series": [{"label": m or "—", "value": n} for m, n in rows]}


async def _study_volume_trend(session, ctx, config):
    since, days = _since(config)
    extra = [StudyRecord.modality == config["modality"].upper()] if config.get("modality") else []
    day = func.date_trunc("day", StudyRecord.created_at)
    rows = (await session.execute(
        select(day, func.count()).where(_studies(ctx, StudyRecord.created_at >= since, *extra))
        .group_by(day).order_by(day)
    )).all()
    by_day = {d.date().isoformat(): n for d, n in rows}
    start = since.date()
    points = []
    for i in range(days + 1):
        key = (start + timedelta(days=i)).isoformat()
        points.append({"label": key, "value": by_day.get(key, 0)})
    return {"series": points}


async def _study_list(session, ctx, config, *criteria, order):
    rows = (await session.execute(
        select(StudyRecord).where(_studies(ctx, *criteria)).order_by(order).limit(_limit(config))
    )).scalars().all()
    return {"rows": [_study_row(r) for r in rows]}


async def _my_worklist(session, ctx, config):
    return await _study_list(
        session, ctx, config,
        StudyRecord.assigned_to == ctx.user_id, StudyRecord.reading_status != "signed",
        order=StudyRecord.created_at.asc(),
    )


async def _pending_signoff(session, ctx, config):
    return await _study_list(
        session, ctx, config, StudyRecord.reading_status == "reported",
        order=StudyRecord.reported_at.asc(),
    )


async def _overdue_studies(session, ctx, config):
    try:
        hours = max(1, int(config.get("hours", 24)))
    except (TypeError, ValueError):
        hours = 24
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return await _study_list(
        session, ctx, config,
        StudyRecord.reading_status.in_(("unread", "in_progress")), StudyRecord.created_at < cutoff,
        order=StudyRecord.created_at.asc(),
    )


async def _recent_reports(session, ctx, config):
    return await _study_list(
        session, ctx, config, StudyRecord.reported_at.isnot(None),
        order=func.coalesce(StudyRecord.signed_at, StudyRecord.reported_at).desc(),
    )


async def _critical_alerts(session, ctx, config):
    scope = and_(
        CriticalAlertRecord.tenant_id == ctx.tenant_id,
        _study_uid_scope(ctx, CriticalAlertRecord.study_instance_uid),
    )
    open_count = (await session.execute(
        select(func.count()).select_from(CriticalAlertRecord).where(
            scope, CriticalAlertRecord.status.in_(_OPEN_ALERT_STATUSES)
        )
    )).scalar_one()
    recent = (await session.execute(
        select(CriticalAlertRecord).where(scope)
        .order_by(CriticalAlertRecord.created_at.desc()).limit(_limit(config))
    )).scalars().all()
    return {
        "value": open_count,
        "rows": [{
            "id": a.id, "title": a.title, "severity": a.severity, "status": a.status,
            "study_instance_uid": a.study_instance_uid, "usecase": a.usecase_name,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        } for a in recent],
    }


async def _turnaround_time(session, ctx, config):
    since, _ = _since(config)

    async def _percentiles(end_col):
        minutes = func.extract("epoch", end_col - StudyRecord.created_at) / 60.0
        row = (await session.execute(
            select(
                func.percentile_cont(0.5).within_group(minutes),
                func.percentile_cont(0.9).within_group(minutes),
                func.count(),
            ).where(_studies(ctx, end_col.isnot(None), end_col >= since))
        )).one()
        return {
            "median_minutes": round(row[0], 1) if row[0] is not None else None,
            "p90_minutes": round(row[1], 1) if row[1] is not None else None,
            "count": row[2],
        }

    return {"report": await _percentiles(StudyRecord.reported_at),
            "signoff": await _percentiles(StudyRecord.signed_at)}


async def _radiologist_workload(session, ctx, config):
    since, _ = _since(config)
    rows = (await session.execute(
        select(
            StudyRecord.assigned_to_username,
            func.sum(case((StudyRecord.reading_status == "in_progress", 1), else_=0)),
            func.sum(case((and_(StudyRecord.reading_status == "signed", StudyRecord.signed_at >= since), 1), else_=0)),
        )
        .where(_studies(ctx, StudyRecord.assigned_to.isnot(None)))
        .group_by(StudyRecord.assigned_to_username)
        .order_by(StudyRecord.assigned_to_username)
    )).all()
    return {"rows": [{"radiologist": name or "—", "in_progress": int(ip or 0), "signed": int(sg or 0)}
                     for name, ip, sg in rows]}


async def _ai_jobs(session, ctx, config):
    since, _ = _since(config)
    scope = and_(
        JobRunRecord.tenant_id == ctx.tenant_id, JobRunRecord.created_at >= since,
        _study_uid_scope(ctx, JobRunRecord.study_instance_uid),
    )
    by_status = (await session.execute(
        select(JobRunRecord.status, func.count()).where(scope).group_by(JobRunRecord.status)
    )).all()
    by_usecase = (await session.execute(
        select(JobRunRecord.usecase_name, func.count()).where(scope)
        .group_by(JobRunRecord.usecase_name).order_by(func.count().desc())
    )).all()
    return {"by_status": [{"label": s, "value": n} for s, n in by_status],
            "series": [{"label": u, "value": n} for u, n in by_usecase]}


async def _ai_qa_flags(session, ctx, config):
    since, _ = _since(config)
    flagged = case((func.coalesce(func.json_array_length(ResultRecord.qa_flags), 0) > 0, 1), else_=0)
    rows = (await session.execute(
        select(ResultRecord.usecase_name, func.count(), func.sum(flagged))
        .where(
            ResultRecord.tenant_id == ctx.tenant_id, ResultRecord.created_at >= since,
            ResultRecord.is_latest == True,  # noqa: E712
            _study_uid_scope(ctx, ResultRecord.study_instance_uid),
        )
        .group_by(ResultRecord.usecase_name)
    )).all()
    return {"rows": [{
        "usecase": u, "results": total, "flagged": int(f or 0),
        "flag_rate": round((int(f or 0) / total) * 100, 1) if total else 0.0,
    } for u, total, f in rows]}


async def _todays_intake(session, ctx, config):
    since, _ = _since(config)
    patients = (await session.execute(
        select(func.count()).select_from(PatientRecord).where(
            PatientRecord.tenant_id == ctx.tenant_id, PatientRecord.created_at >= since
        )
    )).scalar_one()
    orders = (await session.execute(
        select(func.count()).select_from(OrderRecord).where(
            OrderRecord.tenant_id == ctx.tenant_id, OrderRecord.created_at >= since
        )
    )).scalar_one()
    return {"patients": patients, "orders": orders}


async def _markdown(session, ctx, config):
    return {"text": str(config.get("text", ""))[:5000]}


WIDGET_HANDLERS: dict[str, Callable[[Any, WidgetContext, dict], Awaitable[dict]]] = {
    "kpi_studies": _kpi_studies,
    "reading_status": _reading_status,
    "studies_by_modality": _studies_by_modality,
    "study_volume_trend": _study_volume_trend,
    "my_worklist": _my_worklist,
    "pending_signoff": _pending_signoff,
    "overdue_studies": _overdue_studies,
    "critical_alerts": _critical_alerts,
    "turnaround_time": _turnaround_time,
    "radiologist_workload": _radiologist_workload,
    "ai_jobs": _ai_jobs,
    "ai_qa_flags": _ai_qa_flags,
    "todays_intake": _todays_intake,
    "recent_reports": _recent_reports,
    "markdown": _markdown,
}
