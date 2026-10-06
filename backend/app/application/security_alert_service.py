from __future__ import annotations

"""Security alert rules over the audit log (checklist ADM-04 SIEM alerts, PRV-05 detection).

Run every 10 minutes by Celery Beat (``run_security_alerts``) over a slightly overlapping
window; each alert is raised once (de-duplicated by the caller). Alerts go to the
tenant's administrators in real time, to ``security_alert`` webhook rules (only to
BAA-covered destinations, outbound policy), and to the structured log for a SIEM.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

# action -> (kind, severity, title)
SINGLE_EVENT_RULES: dict[str, tuple[str, str, str]] = {
    "account_locked": ("account_locked", "medium", "Account locked after repeated failed sign-ins"),
    "session_reuse_detected": ("session_theft", "high", "Refresh-token reuse: possible session theft"),
    "break_glass_invoked": ("emergency_access", "high", "Emergency (break-glass) access used"),
    "artifact_integrity_violation": ("integrity", "critical", "Stored AI artifact failed its integrity check"),
    "signed_document_integrity_failed": ("integrity", "critical", "Signed report failed its integrity check"),
    "impersonation_started": ("impersonation", "medium", "Platform operator started impersonating a user"),
}
FAILED_SIGN_IN = ("login_failed", "mfa_failed")


@dataclass
class SecurityAlert:
    tenant_id: str
    kind: str
    severity: str
    title: str
    key: str                     # de-duplication key
    count: int = 1
    details: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {"type": "security_alert", "kind": self.kind, "severity": self.severity,
                "title": self.title, "count": self.count, "details": self.details}


class SecurityAlertService:
    def __init__(self, session, failed_login_threshold: int = 10):
        self._session = session
        self._threshold = failed_login_threshold

    async def evaluate(self, start: datetime, end: datetime) -> list[SecurityAlert]:
        from app.infrastructure.database.models import AuditLogRecord as A

        alerts: list[SecurityAlert] = []
        rows = (await self._session.execute(
            select(A).where(A.timestamp >= start, A.timestamp < end,
                            A.action.in_(tuple(SINGLE_EVENT_RULES)))
        )).scalars().all()
        for r in rows:
            kind, severity, title = SINGLE_EVENT_RULES[r.action]
            alerts.append(SecurityAlert(
                tenant_id=r.tenant_id, kind=kind, severity=severity, title=title, key=f"row:{r.id}",
                details={"action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
                         "actor": r.actor_display or r.actor, "client_ip": r.client_ip,
                         "at": r.timestamp.isoformat() if r.timestamp else None},
            ))
        bursts = (await self._session.execute(
            select(A.tenant_id, A.client_ip, func.count())
            .where(A.timestamp >= start, A.timestamp < end, A.action.in_(FAILED_SIGN_IN))
            .group_by(A.tenant_id, A.client_ip)
            .having(func.count() >= self._threshold)
        )).all()
        for tenant_id, ip, n in bursts:
            alerts.append(SecurityAlert(
                tenant_id=tenant_id, kind="failed_sign_in_burst", severity="high",
                title=f"{n} failed sign-ins from one address in {int((end - start).total_seconds() // 60)} min",
                key=f"burst:{tenant_id}:{ip}:{end:%Y%m%d%H}", count=int(n),
                details={"client_ip": ip or "unknown"},
            ))
        return alerts
