"""Analytics Service — QA metrics, capacity planning, longitudinal trends, urgency scoring."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


def _naive_utc_now() -> datetime:
    """Return current UTC time without tzinfo (matches DB TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.utcnow()


def _since(days: int) -> datetime:
    return _naive_utc_now() - timedelta(days=days)


class AnalyticsService:
    def __init__(self, session: AsyncSession):
        self._session = session

    # ── QA / Audit Metrics ────────────────────────────────────────────────────

    async def get_qa_metrics(
        self,
        days: int = 30,
        usecase_name: str | None = None,
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import JobRunRecord, ResultRecord, ReviewQueueRecord

        since = _since(days)

        job_stmt = (
            select(JobRunRecord)
            .where(
                JobRunRecord.status == "completed",
                JobRunRecord.created_at >= since,
                JobRunRecord.started_at.isnot(None),
                JobRunRecord.completed_at.isnot(None),
            )
        )
        if usecase_name:
            job_stmt = job_stmt.where(JobRunRecord.usecase_name == usecase_name)

        job_result = await self._session.execute(job_stmt)
        jobs = job_result.scalars().all()

        tat_seconds: list[float] = []
        usecase_tat: dict[str, list[float]] = {}
        for job in jobs:
            if job.started_at and job.completed_at:
                delta = (job.completed_at - job.started_at).total_seconds()
                if delta > 0:
                    tat_seconds.append(delta)
                    usecase_tat.setdefault(job.usecase_name, []).append(delta)

        def _percentile(data: list[float], p: int) -> float:
            if not data:
                return 0.0
            sorted_data = sorted(data)
            idx = int(len(sorted_data) * p / 100)
            return round(sorted_data[min(idx, len(sorted_data) - 1)] / 60, 1)

        tat_median = round(statistics.median(tat_seconds) / 60, 1) if tat_seconds else 0.0
        tat_p75 = _percentile(tat_seconds, 75)
        tat_p95 = _percentile(tat_seconds, 95)

        tat_by_usecase: dict[str, float] = {}
        for uc, vals in usecase_tat.items():
            tat_by_usecase[uc] = round(statistics.median(vals) / 60, 1)

        # Review queue stats
        rq_stmt = (
            select(ReviewQueueRecord.status, func.count(ReviewQueueRecord.id).label("cnt"))
            .where(ReviewQueueRecord.created_at >= since)
            .group_by(ReviewQueueRecord.status)
        )
        rq_result = await self._session.execute(rq_stmt)
        review_stats: dict[str, int] = {row.status: row.cnt for row in rq_result}

        total_reviewed = review_stats.get("approved", 0) + review_stats.get("corrected", 0)
        corrected = review_stats.get("corrected", 0)
        correction_rate = round((corrected / total_reviewed) * 100, 1) if total_reviewed > 0 else 0.0

        # QA flag rate
        result_stmt = select(ResultRecord).where(ResultRecord.created_at >= since)
        if usecase_name:
            result_stmt = result_stmt.where(ResultRecord.usecase_name == usecase_name)
        result_res = await self._session.execute(result_stmt)
        all_results = result_res.scalars().all()
        total_results = len(all_results)
        results_with_flags = sum(1 for r in all_results if r.qa_flags)
        qa_flag_rate = round((results_with_flags / total_results) * 100, 1) if total_results > 0 else 0.0

        # Failed jobs count
        failed_stmt = select(func.count(JobRunRecord.id)).where(
            JobRunRecord.status == "failed",
            JobRunRecord.created_at >= since,
        )
        failed_res = await self._session.execute(failed_stmt)
        failed_count = failed_res.scalar_one() or 0

        return {
            "tat_median_minutes": tat_median,
            "tat_p75_minutes": tat_p75,
            "tat_p95_minutes": tat_p95,
            "tat_by_usecase": tat_by_usecase,
            "review_queue_stats": review_stats,
            "correction_rate_pct": correction_rate,
            "qa_flag_rate_pct": qa_flag_rate,
            "jobs_completed": len(jobs),
            "jobs_failed": int(failed_count),
        }

    # ── Capacity / Utilization ────────────────────────────────────────────────

    async def get_capacity_metrics(self, days: int = 30) -> dict[str, Any]:
        from app.infrastructure.database.models import JobRunRecord

        since = _since(days)

        stmt = (
            select(JobRunRecord)
            .where(
                JobRunRecord.status == "completed",
                JobRunRecord.created_at >= since,
            )
            .order_by(JobRunRecord.created_at)
        )
        result = await self._session.execute(stmt)
        jobs = result.scalars().all()

        daily_map: dict[str, dict[str, int]] = {}
        hourly_volume: list[int] = [0] * 24
        usecase_duration: dict[str, list[float]] = {}

        for job in jobs:
            day_key = job.created_at.strftime("%Y-%m-%d") if job.created_at else "unknown"
            daily_map.setdefault(day_key, {})
            daily_map[day_key][job.usecase_name] = daily_map[day_key].get(job.usecase_name, 0) + 1

            if job.created_at:
                hourly_volume[job.created_at.hour] += 1

            if job.started_at and job.completed_at:
                dur = (job.completed_at - job.started_at).total_seconds()
                if dur > 0:
                    usecase_duration.setdefault(job.usecase_name, []).append(dur)

        avg_duration: dict[str, float] = {
            uc: round(sum(durs) / len(durs) / 60, 1)
            for uc, durs in usecase_duration.items() if durs
        }

        # Build ordered daily_volume list
        daily_volume_list = []
        for d_key in sorted(daily_map.keys()):
            total = sum(daily_map[d_key].values())
            daily_volume_list.append({
                "date": d_key,
                "total": total,
                "by_usecase": daily_map[d_key],
            })

        # Last 7 days actual
        last_7: list[int] = []
        for i in range(7, 0, -1):
            day = (_naive_utc_now() - timedelta(days=i)).strftime("%Y-%m-%d")
            last_7.append(sum(daily_map.get(day, {}).values()))

        # 7-day exponential smoothing forecast
        alpha = 0.4
        smoothed = float(last_7[0]) if last_7 else 0.0
        for v in last_7[1:]:
            smoothed = alpha * v + (1 - alpha) * smoothed
        forecast_7day = [round(smoothed) for _ in range(7)]

        peak_hour = int(max(range(24), key=lambda h: hourly_volume[h])) if any(hourly_volume) else 0

        return {
            "daily_volume": daily_volume_list,
            "hourly_heatmap": hourly_volume,
            "peak_hour": peak_hour,
            "avg_duration_by_usecase": avg_duration,
            "forecast_7day": forecast_7day,
            "last_7days_actual": last_7,
        }

    # ── Longitudinal Trend ────────────────────────────────────────────────────

    async def get_patient_trend(self, patient_id: str, usecase_name: str) -> dict[str, Any]:
        from app.infrastructure.database.models import ResultRecord, StudyRecord

        study_stmt = select(StudyRecord).where(StudyRecord.patient_id == patient_id)
        study_res = await self._session.execute(study_stmt)
        studies = study_res.scalars().all()

        if not studies:
            return {"patient_id": patient_id, "usecase_name": usecase_name, "timepoints": []}

        study_map = {s.study_instance_uid: s for s in studies}
        study_uids = list(study_map.keys())

        result_stmt = (
            select(ResultRecord)
            .where(
                ResultRecord.study_instance_uid.in_(study_uids),
                ResultRecord.usecase_name == usecase_name,
                ResultRecord.is_latest == True,
            )
            .order_by(ResultRecord.created_at)
        )
        result_res = await self._session.execute(result_stmt)
        results = result_res.scalars().all()

        timepoints = []
        for r in results:
            study = study_map.get(r.study_instance_uid)
            study_date = study.study_date if study else None
            timepoints.append({
                "result_id": r.id,
                "study_instance_uid": r.study_instance_uid,
                "study_date": study_date.isoformat() if study_date else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "measurements": r.measurements or {},
                "qa_flags": r.qa_flags or [],
                "rano_classification": None,
            })

        timepoints.sort(key=lambda x: x["study_date"] or x["created_at"] or "")
        timepoints = _classify_rano(timepoints, usecase_name)

        return {
            "patient_id": patient_id,
            "usecase_name": usecase_name,
            "timepoints": timepoints,
        }

    # ── Worklist Urgency Scoring ───────────────────────────────────────────────

    async def compute_urgency_scores(self, study_uids: list[str]) -> list[dict[str, Any]]:
        """Return urgency score objects with priority label per study UID."""
        from app.infrastructure.database.models import JobRunRecord, ResultRecord, StudyRecord

        if not study_uids:
            return []

        result_stmt = select(ResultRecord).where(
            ResultRecord.study_instance_uid.in_(study_uids),
            ResultRecord.is_latest == True,
        )
        result_res = await self._session.execute(result_stmt)
        results = result_res.scalars().all()

        job_stmt = select(JobRunRecord).where(JobRunRecord.study_instance_uid.in_(study_uids))
        job_res = await self._session.execute(job_stmt)
        jobs = job_res.scalars().all()

        study_stmt = select(StudyRecord).where(StudyRecord.study_instance_uid.in_(study_uids))
        study_res = await self._session.execute(study_stmt)
        studies = study_res.scalars().all()

        now_naive = _naive_utc_now()

        result_map: dict[str, list] = {}
        for r in results:
            result_map.setdefault(r.study_instance_uid, []).append(r)

        job_map: dict[str, list] = {}
        for j in jobs:
            job_map.setdefault(j.study_instance_uid, []).append(j)

        study_age_map: dict[str, float] = {}
        for s in studies:
            ts = s.created_at
            if ts:
                age_hours = (now_naive - ts).total_seconds() / 3600
                study_age_map[s.study_instance_uid] = max(0.0, age_hours)

        scores = []
        for uid in study_uids:
            severity_score = 0.0
            confidence_score = 0.0
            age_score = 0.0
            uid_results = result_map.get(uid, [])
            uid_jobs = job_map.get(uid, [])
            age_h = study_age_map.get(uid, 0)

            # Severity (0–40): based on tumor volume + QA flags
            for r in uid_results[:1]:
                flat = _flatten_measurements(r.measurements or {})
                vol = flat.get("whole_tumor_volume_ml", 0)
                if vol > 100:
                    severity_score = 40
                elif vol > 50:
                    severity_score = 25
                elif vol > 10:
                    severity_score = 15
                elif vol > 0:
                    severity_score = 5
                if r.qa_flags:
                    severity_score = min(40, severity_score + len(r.qa_flags) * 3)

            # AI confidence penalty (0–25): failed/pending jobs
            for j in uid_jobs[:1]:
                if j.status == "failed":
                    confidence_score = 25
                elif j.status in ("pending", "preprocessing"):
                    confidence_score = 10

            # Age (0–20): older unread studies are higher priority
            if age_h > 48:
                age_score = 20
            elif age_h > 24:
                age_score = 12
            elif age_h > 8:
                age_score = 5

            total = min(100.0, round(severity_score + confidence_score + age_score, 1))

            if total >= 70:
                priority = "STAT"
            elif total >= 40:
                priority = "HIGH"
            elif total >= 15:
                priority = "NORMAL"
            else:
                priority = "ROUTINE"

            scores.append({
                "study_instance_uid": uid,
                "score": total,
                "priority": priority,
                "factors": {
                    "severity": severity_score,
                    "confidence": confidence_score,
                    "age": age_score,
                },
            })

        return scores


def _flatten_measurements(d: dict, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
        elif isinstance(v, dict):
            out.update(_flatten_measurements(v, key))
    return out


def _classify_rano(timepoints: list[dict], usecase_name: str) -> list[dict]:
    if usecase_name != "brain_mri" or len(timepoints) < 2:
        return timepoints

    baseline_vol: float | None = None
    nadir_vol = float("inf")

    for i, tp in enumerate(timepoints):
        flat = _flatten_measurements(tp.get("measurements", {}))
        wt = flat.get("whole_tumor_volume_ml", 0.0)

        if i == 0:
            baseline_vol = wt
            nadir_vol = wt
            tp["rano_classification"] = None
            continue

        nadir_vol = min(nadir_vol, wt)

        if baseline_vol and baseline_vol > 0:
            pct_from_nadir = ((wt - nadir_vol) / nadir_vol * 100) if nadir_vol > 0 else 0
            pct_from_baseline = (wt - baseline_vol) / baseline_vol * 100

            if wt < 0.001:
                rano = "CR"
            elif pct_from_nadir >= 25:
                rano = "PD"
            elif pct_from_baseline <= -50:
                rano = "PR"
            else:
                rano = "SD"
        else:
            rano = None

        tp["rano_classification"] = rano

    return timepoints
