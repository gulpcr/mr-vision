from __future__ import annotations

"""Audit every read of patient data (HIPAA 164.312(b) audit controls).

One ``phi_accessed`` entry — who, which resource, which route, from where — per
successful GET of a patient-data route. To keep the log reviewable rather than flooded
by polling, image tiles and slice scrolling, the same user reading the same resource
through the same route is recorded at most once per ``_DEDUPE_SECONDS`` (Redis; if Redis
is unavailable every read is recorded — fail-safe for the audit, not for noise).

Routes that already write their own, more specific entry (result_viewed,
report_downloaded, share_link_redeemed, result_exported) are not repeated here.
Viewer (OHIF / DICOMweb) image access is audited at the nginx auth_request hook
(``viewer_access.dicomweb_authz``), the only place that sees it.
"""

import re
from dataclasses import dataclass

import redis.asyncio as aioredis

from app.infrastructure.redis_conn import async_redis
import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


logger = structlog.get_logger(__name__)

_DEDUPE_SECONDS = 15 * 60
_UID = r"(?P<id>[^/]+)"


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern
    entity_type: str
    route: str


def _r(regex: str, entity_type: str, route: str) -> _Rule:
    return _Rule(re.compile(rf"^{regex}$"), entity_type, route)


# Order matters: the first match wins.
_RULES: tuple[_Rule, ...] = (
    # Already audited with a specific action by the route itself.
    _r(r"/api/results/[^/]+/[^/]+", "skip", "results.get"),
    _r(r"/api/results/[^/]+/report\.pdf", "skip", "results.report_pdf"),
    _r(r"/api/results/[^/]+/[^/]+/flagged-slices", "skip", "results.flagged_slices"),
    _r(r"/api/portal/[^/]+", "skip", "portal"),
    _r(r"/api/reports/[^/]+/[^/]+/fhir", "skip", "reports.fhir"),
    # Studies and everything hanging off one.
    _r(r"/api/studies", "study_list", "studies.list"),
    _r(rf"/api/studies/{_UID}(?:/(?P<sub>clinical|comments|jobs|signoff|routing-preview|mammography-report(?:\.pdf)?))?",
       "study", "studies"),
    _r(rf"/api/reports/{_UID}/[^/]+/(?P<sub>pdf|consolidated-report|narrative|clinical-context|longitudinal|dicom-sr)",
       "study", "reports"),
    _r(r"/api/reports/worklist", "study_list", "reports.worklist"),
    _r(rf"/api/results/{_UID}", "study", "results.list"),
    _r(rf"/api/results/{_UID}/[^/]+/(?P<sub>versions|cpt-suggestions)", "study", "results"),
    _r(rf"/api/results/{_UID}/shares", "result", "results.shares"),
    _r(rf"/api/artifacts/{_UID}/.+", "study", "artifacts"),
    _r(rf"/api/preview/{_UID}/.+", "study", "preview"),
    _r(rf"/api/fused/{_UID}/.+", "study", "fused"),
    _r(r"/api/patients", "patient_list", "patients.list"),
    _r(rf"/api/patients/{_UID}", "patient", "patients"),
    _r(r"/api/clinical/(?P<sub>conditions|observations)", "clinical_list", "clinical"),
    _r(r"/api/review-queue", "study_list", "review_queue"),
    _r(r"/api/admin/review(?:/[^/]+|/stats)?", "review", "admin.review"),
    _r(rf"/api/admin/patients/{_UID}/trend/[^/]+", "patient", "admin.patient_trend"),
    _r(rf"/api/admin/studies/{_UID}/(?P<sub>prior-comparison/[^/]+|protocol-check)", "study", "admin.study"),
    _r(r"/api/critical-alerts(?:/[^/]+)?", "critical_alert", "critical_alerts"),
    _r(r"/api/orthanc/studies", "study_list", "orthanc.studies"),
    _r(rf"/api/debug/medgemma/[^/]+/{_UID}", "study", "debug.medgemma"),
    _r(r"/api/admin/(?:platform/)?audit", "audit_log", "admin.audit"),
)


def _match(path: str) -> tuple[_Rule, str] | None:
    for rule in _RULES:
        m = rule.pattern.match(path)
        if m:
            groups = m.groupdict()
            entity_id = groups.get("id") or "*"
            return rule, entity_id
    return None


def _redis() -> aioredis.Redis:
    return async_redis(socket_timeout=2)


async def _first_in_window(key: str) -> bool:
    try:
        return bool(await _redis().set(key, "1", nx=True, ex=_DEDUPE_SECONDS))
    except Exception as exc:
        logger.warning("phi_audit_dedupe_unavailable", error=str(exc))
        return True


async def audit_phi_access(
    *, user_id: str, username: str, tenant_id: str, client_ip: str,
    entity_type: str, entity_id: str, route: str,
) -> None:
    """Write one (de-duplicated) phi_accessed entry in its own transaction."""
    if not user_id:
        return
    if not await _first_in_window(f"phi_audit:{user_id}:{route}:{entity_type}:{entity_id}"):
        return
    from app.application.audit_service import AuditService
    from app.domain.enums import AuditAction
    from app.infrastructure.database.session import async_session_factory
    from app.infrastructure.tenant.db_scope import tenant_scope

    try:
        with tenant_scope(tenant_id or "default"):
            async with async_session_factory() as session:
                await AuditService(session).record(
                    AuditAction.PHI_ACCESSED.value, entity_type, entity_id,
                    actor_id=user_id, actor_display=username, client_ip=client_ip,
                    details={"route": route}, commit=True,
                )
    except Exception as exc:  # never fail the read because its audit entry failed
        logger.warning("phi_audit_write_failed", route=route, error=str(exc))


class PhiReadAuditMiddleware(BaseHTTPMiddleware):
    """Records successful GETs of patient-data routes (see module docstring). Must run
    inside RBACMiddleware so ``request.state`` carries the authenticated user."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.method != "GET" or response.status_code >= 400:
            return response
        hit = _match(request.url.path)
        if hit is None or hit[0].entity_type == "skip":
            return response
        rule, entity_id = hit
        from app.application.audit_service import client_ip_from

        await audit_phi_access(
            user_id=getattr(request.state, "user_id", "") or "",
            username=getattr(request.state, "user", "") or "",
            tenant_id=getattr(request.state, "tenant_id", "default") or "default",
            client_ip=client_ip_from(request),
            entity_type=rule.entity_type,
            entity_id=entity_id,
            route=rule.route,
        )
        return response
