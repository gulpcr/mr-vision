from __future__ import annotations

import uuid
from typing import Any

import structlog
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = structlog.get_logger(__name__)


class AlertingService:
    """Manages alert rules and sends webhook notifications."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_rule(
        self,
        name: str,
        event_type: str,
        webhook_url: str,
        condition: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import AlertRuleRecord

        rule_id = str(uuid.uuid4())
        record = AlertRuleRecord(
            id=rule_id,
            name=name,
            event_type=event_type,
            condition=condition or {},
            webhook_url=webhook_url,
            is_active=True,
            tenant_id=tenant_id,
        )
        self._session.add(record)
        await self._session.flush()
        return {
            "id": rule_id,
            "name": name,
            "event_type": event_type,
            "webhook_url": webhook_url,
            "is_active": True,
        }

    async def list_rules(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import AlertRuleRecord

        stmt = select(AlertRuleRecord).order_by(AlertRuleRecord.created_at.desc())
        if tenant_id:
            stmt = stmt.where(AlertRuleRecord.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "name": r.name,
                "event_type": r.event_type,
                "condition": r.condition,
                "webhook_url": r.webhook_url,
                "is_active": r.is_active,
                "tenant_id": r.tenant_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    async def delete_rule(self, rule_id: str) -> bool:
        from app.infrastructure.database.models import AlertRuleRecord
        from sqlalchemy import delete

        stmt = delete(AlertRuleRecord).where(AlertRuleRecord.id == rule_id)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def trigger_alert(
        self, event_type: str, payload: dict[str, Any]
    ) -> int:
        """Check rules matching event_type and send webhooks."""
        from app.infrastructure.database.models import AlertRuleRecord, AlertHistoryRecord

        stmt = select(AlertRuleRecord).where(
            AlertRuleRecord.event_type == event_type,
            AlertRuleRecord.is_active == True,
        )
        result = await self._session.execute(stmt)
        rules = result.scalars().all()

        sent_count = 0
        for rule in rules:
            if not self._matches_condition(rule.condition, payload):
                continue

            success = await self._send_webhook(rule.webhook_url, event_type, payload)

            history = AlertHistoryRecord(
                id=str(uuid.uuid4()),
                rule_id=rule.id,
                event_type=event_type,
                payload=payload,
                status="sent" if success else "failed",
            )
            self._session.add(history)

            if success:
                sent_count += 1

        await self._session.flush()
        return sent_count

    async def get_history(
        self, rule_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import AlertHistoryRecord

        stmt = select(AlertHistoryRecord).order_by(
            AlertHistoryRecord.created_at.desc()
        ).limit(limit)
        if rule_id:
            stmt = stmt.where(AlertHistoryRecord.rule_id == rule_id)
        result = await self._session.execute(stmt)
        return [
            {
                "id": r.id,
                "rule_id": r.rule_id,
                "event_type": r.event_type,
                "payload": r.payload,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in result.scalars().all()
        ]

    @staticmethod
    def _matches_condition(
        condition: dict[str, Any] | None, payload: dict[str, Any]
    ) -> bool:
        """Evaluate a condition against a payload.

        Supports two condition formats:
        1. Simple equality dict: {"key": "value", ...}
        2. Threshold list: [{"key": "...", "op": "gt|gte|lt|lte|eq|contains", "value": ...}, ...]
        """
        if not condition:
            return True

        # New format: list of threshold rules (all must match = AND logic)
        if isinstance(condition, list):
            return all(
                AlertingService._eval_rule(rule, payload) for rule in condition
            )

        # Legacy format: simple equality dict
        for key, expected in condition.items():
            actual = payload
            # Support dot-notation keys: "measurements.whole_tumor_volume_ml"
            for part in key.split("."):
                if isinstance(actual, dict):
                    actual = actual.get(part)
                else:
                    actual = None
                    break
            if actual is None or actual != expected:
                return False
        return True

    @staticmethod
    def _eval_rule(rule: dict[str, Any], payload: dict[str, Any]) -> bool:
        key = rule.get("key", "")
        op = rule.get("op", "eq")
        expected = rule.get("value")

        # Traverse dot-notation into nested payload
        actual = payload
        for part in key.split("."):
            if isinstance(actual, dict):
                actual = actual.get(part)
            else:
                actual = None
                break

        if actual is None:
            return False

        try:
            if op == "eq":
                return actual == expected
            if op == "neq":
                return actual != expected
            if op == "gt":
                return float(actual) > float(expected)
            if op == "gte":
                return float(actual) >= float(expected)
            if op == "lt":
                return float(actual) < float(expected)
            if op == "lte":
                return float(actual) <= float(expected)
            if op == "contains":
                return str(expected) in str(actual)
            if op == "in":
                return actual in expected
        except (TypeError, ValueError):
            pass
        return False

    async def evaluate_result_alerts(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result_id: str,
        measurements: dict[str, Any],
        summary: dict[str, Any],
        qa_flags: list[str],
        patient_id: str | None = None,
    ) -> int:
        """Check webhook rules and critical finding rules against a pipeline result."""
        flat_measurements: dict[str, Any] = {}
        self._flatten_into(measurements, flat_measurements)
        self._flatten_into(summary, flat_measurements, prefix="summary")

        payload = {
            "study_instance_uid": study_instance_uid,
            "usecase_name": usecase_name,
            "result_id": result_id,
            "patient_id": patient_id or "",
            "qa_flags": qa_flags,
            "measurements": measurements,
            **{f"measurements.{k}": v for k, v in flat_measurements.items()},
        }
        webhook_count = await self.trigger_alert("result_ready", payload)

        # Critical finding detection
        findings = self._check_critical_findings(usecase_name, summary, measurements, qa_flags)
        if findings:
            await self._persist_and_dispatch_critical_alerts(
                study_instance_uid=study_instance_uid,
                usecase_name=usecase_name,
                result_id=result_id,
                patient_id=patient_id,
                findings=findings,
            )

        return webhook_count + len(findings)

    @staticmethod
    def _check_critical_findings(
        usecase_name: str,
        summary: dict[str, Any],
        measurements: dict[str, Any],
        qa_flags: list[str],
    ) -> list[dict[str, Any]]:
        """Return list of {finding_type, severity, title, message, details} dicts."""
        findings: list[dict[str, Any]] = []

        def _add(finding_type: str, severity: str, title: str, message: str, details: dict):
            findings.append({
                "finding_type": finding_type,
                "severity": severity,
                "title": title,
                "message": message,
                "details": details,
            })

        # ── PET/CT oncology ──────────────────────────────────────────────────
        if usecase_name == "pet_ct":
            suvmax = summary.get("suvmax_body") or measurements.get("whole_body", {}).get("suvmax_body")
            lesion_count = summary.get("lesion_count", 0)
            mtv = summary.get("mtv_total_ml") or measurements.get("whole_body", {}).get("mtv_total_ml")
            deauville = summary.get("deauville_score")
            percist = summary.get("percist_score")

            if suvmax is not None:
                if float(suvmax) >= 20:
                    _add("extreme_suvmax", "CRITICAL",
                         "Extreme whole-body SUVmax ≥20",
                         f"Whole-body SUVmax {suvmax:.1f} — extremely high FDG uptake indicating possible aggressive malignancy. Urgent oncology review recommended.",
                         {"suvmax_body": suvmax})
                elif float(suvmax) >= 15:
                    _add("high_suvmax", "WARNING",
                         "High whole-body SUVmax ≥15",
                         f"Whole-body SUVmax {suvmax:.1f} — markedly elevated FDG uptake consistent with highly metabolically active tumour.",
                         {"suvmax_body": suvmax})

            if deauville is not None and int(deauville) == 5:
                _add("deauville_5", "CRITICAL",
                     "Deauville Score 5 — Progressive Metabolic Disease",
                     "Deauville score 5: new FDG-avid lesion(s) or markedly increased uptake. Consistent with progressive metabolic disease (PMD) per PERCIST 1.0.",
                     {"deauville_score": deauville, "percist_score": percist})

            elif percist == "PMD":
                _add("percist_pmd", "WARNING",
                     "PERCIST Progressive Metabolic Disease",
                     "PERCIST 1.0 classification: Progressive Metabolic Disease (PMD). ≥30% increase in SUL or new FDG-avid lesion.",
                     {"percist_score": percist})

            if lesion_count >= 10:
                _add("disseminated_disease", "WARNING",
                     f"Disseminated FDG-avid disease — {lesion_count} lesions",
                     f"{lesion_count} FDG-avid lesions detected across the body, suggesting disseminated/metastatic disease.",
                     {"lesion_count": lesion_count})

            if mtv is not None and float(mtv) >= 500:
                _add("high_mtv", "WARNING",
                     f"High metabolic tumour volume {mtv:.0f} mL",
                     f"Total metabolic tumour volume {mtv:.1f} mL (≥500 mL). High MTV is associated with poor prognosis.",
                     {"mtv_total_ml": mtv})

        # ── Brain PET/CT ─────────────────────────────────────────────────────
        elif usecase_name == "pet_ct_brain":
            amyloid_pos = summary.get("amyloid_positive")
            centiloid = summary.get("centiloid")
            global_suvr = summary.get("global_suvr")

            if amyloid_pos is True:
                _add("amyloid_positive", "WARNING",
                     "Amyloid PET Positive",
                     "Amyloid PET scan is positive — consistent with cerebral amyloid plaque burden. Consider correlation with clinical presentation and cognitive assessment.",
                     {"centiloid": centiloid, "global_suvr": global_suvr})

            if centiloid is not None and float(centiloid) >= 50:
                _add("high_centiloid", "WARNING",
                     f"High amyloid load — Centiloid {centiloid:.0f}",
                     f"Centiloid value {centiloid:.1f} (≥50) indicates high amyloid burden. Associated with increased risk of cognitive decline.",
                     {"centiloid": centiloid})

            if global_suvr is not None and float(global_suvr) >= 2.0 and not amyloid_pos:
                _add("high_global_suvr", "WARNING",
                     f"High global SUVR {global_suvr:.2f}",
                     f"Global SUVR {global_suvr:.2f} (≥2.0) — unexpectedly high tracer uptake. Review for image quality issues or abnormal metabolism.",
                     {"global_suvr": global_suvr})

        # ── Generic brain MRI (whole tumour volume) ──────────────────────────
        elif "brain" in usecase_name or "glioma" in usecase_name or "tumor" in usecase_name:
            wtv = (measurements.get("whole_tumor_volume_ml")
                   or measurements.get("volumes", {}).get("whole_tumor_volume_ml"))
            if wtv is not None:
                if float(wtv) >= 100:
                    _add("large_tumor_volume", "CRITICAL",
                         f"Large tumour volume {wtv:.0f} mL",
                         f"Whole tumour volume {wtv:.1f} mL (≥100 mL) — large intracranial mass. Urgent neurosurgical review recommended.",
                         {"whole_tumor_volume_ml": wtv})
                elif float(wtv) >= 50:
                    _add("significant_tumor_volume", "WARNING",
                         f"Significant tumour volume {wtv:.0f} mL",
                         f"Whole tumour volume {wtv:.1f} mL (≥50 mL) — significant mass burden requiring close follow-up.",
                         {"whole_tumor_volume_ml": wtv})

        # ── QA error flags (all use cases) ───────────────────────────────────
        error_flags = [f for f in qa_flags if any(
            err_kw in f for err_kw in ("error", "missing", "critical", "insufficient", "weight")
        )]
        if error_flags:
            _add("qa_errors", "WARNING",
                 f"QA Errors: {len(error_flags)} flag(s)",
                 f"Pipeline QA detected {len(error_flags)} error flag(s): {', '.join(error_flags)}. Results may be unreliable.",
                 {"qa_error_flags": error_flags})

        return findings

    async def _persist_and_dispatch_critical_alerts(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result_id: str,
        patient_id: str | None,
        findings: list[dict[str, Any]],
    ) -> None:
        from app.infrastructure.database.models import CriticalAlertRecord

        new_alerts = []
        for finding in findings:
            alert_id = str(uuid.uuid4())
            record = CriticalAlertRecord(
                id=alert_id,
                study_instance_uid=study_instance_uid,
                usecase_name=usecase_name,
                result_id=result_id,
                patient_id=patient_id,
                finding_type=finding["finding_type"],
                severity=finding["severity"],
                title=finding["title"],
                message=finding["message"],
                details=finding.get("details", {}),
                status="pending",
                notification_channels=["websocket"],
            )
            self._session.add(record)
            new_alerts.append((alert_id, finding))

        await self._session.flush()

        # Dispatch WebSocket push for each critical finding
        for alert_id, finding in new_alerts:
            try:
                from app.interface.api.ws import manager
                await manager.broadcast({
                    "type": "critical_finding",
                    "alert_id": alert_id,
                    "severity": finding["severity"],
                    "title": finding["title"],
                    "message": finding["message"],
                    "finding_type": finding["finding_type"],
                    "study_instance_uid": study_instance_uid,
                    "usecase_name": usecase_name,
                    "result_id": result_id,
                    "patient_id": patient_id or "",
                })
            except Exception as e:
                logger.warning("critical_alert_ws_dispatch_failed", error=str(e))

    async def list_critical_alerts(
        self,
        status: str | None = None,
        severity: str | None = None,
        usecase_name: str | None = None,
        patient_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import CriticalAlertRecord
        from sqlalchemy import desc

        stmt = select(CriticalAlertRecord).order_by(desc(CriticalAlertRecord.created_at))
        if status:
            stmt = stmt.where(CriticalAlertRecord.status == status)
        if severity:
            stmt = stmt.where(CriticalAlertRecord.severity == severity)
        if usecase_name:
            stmt = stmt.where(CriticalAlertRecord.usecase_name == usecase_name)
        if patient_id:
            stmt = stmt.where(CriticalAlertRecord.patient_id == patient_id)
        stmt = stmt.offset(offset).limit(limit)

        result = await self._session.execute(stmt)
        return [self._alert_to_dict(r) for r in result.scalars().all()]

    async def get_critical_alert(self, alert_id: str) -> dict[str, Any] | None:
        from app.infrastructure.database.models import CriticalAlertRecord

        stmt = select(CriticalAlertRecord).where(CriticalAlertRecord.id == alert_id)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        return self._alert_to_dict(record) if record else None

    async def acknowledge_critical_alert(
        self, alert_id: str, acknowledged_by: str
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import CriticalAlertRecord
        from datetime import datetime, timezone

        stmt = select(CriticalAlertRecord).where(CriticalAlertRecord.id == alert_id)
        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            return None
        record.status = "acknowledged"
        record.acknowledged_at = datetime.now(timezone.utc)
        record.acknowledged_by = acknowledged_by
        await self._session.flush()
        return self._alert_to_dict(record)

    async def get_critical_alert_stats(self, tenant_id: str | None = None) -> dict[str, Any]:
        from app.infrastructure.database.models import CriticalAlertRecord
        from sqlalchemy import func as sqlfunc

        stmt = select(
            CriticalAlertRecord.status,
            CriticalAlertRecord.severity,
            sqlfunc.count().label("cnt"),
        ).group_by(CriticalAlertRecord.status, CriticalAlertRecord.severity)
        result = await self._session.execute(stmt)
        rows = result.all()

        stats: dict[str, Any] = {
            "pending_critical": 0,
            "pending_warning": 0,
            "total_unacknowledged": 0,
            "total_acknowledged": 0,
            "total_escalated": 0,
        }
        for row in rows:
            status, severity, cnt = row.status, row.severity, row.cnt
            if status == "pending":
                stats["total_unacknowledged"] += cnt
                if severity == "CRITICAL":
                    stats["pending_critical"] += cnt
                elif severity == "WARNING":
                    stats["pending_warning"] += cnt
            elif status == "acknowledged":
                stats["total_acknowledged"] += cnt
            elif status == "escalated":
                stats["total_escalated"] += cnt
                stats["total_unacknowledged"] += cnt
        return stats

    async def escalate_overdue_alerts(self, threshold_minutes: int = 30) -> int:
        """Mark pending CRITICAL alerts as escalated if unacknowledged past threshold."""
        from app.infrastructure.database.models import CriticalAlertRecord
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
        stmt = select(CriticalAlertRecord).where(
            CriticalAlertRecord.status == "pending",
            CriticalAlertRecord.severity == "CRITICAL",
            CriticalAlertRecord.created_at <= cutoff,
        )
        result = await self._session.execute(stmt)
        records = result.scalars().all()
        now = datetime.now(timezone.utc)
        for record in records:
            record.status = "escalated"
            record.escalated_at = now
            record.escalation_count = (record.escalation_count or 0) + 1
            try:
                from app.interface.api.ws import manager
                await manager.broadcast({
                    "type": "critical_finding_escalated",
                    "alert_id": record.id,
                    "severity": record.severity,
                    "title": record.title,
                    "study_instance_uid": record.study_instance_uid,
                    "escalation_count": record.escalation_count,
                })
            except Exception:
                pass

        await self._session.flush()
        return len(records)

    @staticmethod
    def _alert_to_dict(r: Any) -> dict[str, Any]:
        return {
            "id": r.id,
            "study_instance_uid": r.study_instance_uid,
            "usecase_name": r.usecase_name,
            "result_id": r.result_id,
            "patient_id": r.patient_id,
            "finding_type": r.finding_type,
            "severity": r.severity,
            "title": r.title,
            "message": r.message,
            "details": r.details,
            "status": r.status,
            "notification_channels": r.notification_channels,
            "acknowledged_at": r.acknowledged_at.isoformat() if r.acknowledged_at else None,
            "acknowledged_by": r.acknowledged_by,
            "escalated_at": r.escalated_at.isoformat() if r.escalated_at else None,
            "escalation_count": r.escalation_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    @staticmethod
    def _flatten_into(d: dict, out: dict, prefix: str = "") -> None:
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                AlertingService._flatten_into(v, out, key)
            else:
                out[key] = v

    @staticmethod
    async def _send_webhook(
        url: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={"event_type": event_type, "payload": payload},
                    headers={"Content-Type": "application/json"},
                )
                return response.status_code < 400
        except Exception as e:
            logger.error("webhook_send_failed", url=url, error=str(e))
            return False
