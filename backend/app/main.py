from __future__ import annotations

import structlog
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.application.routing_service import RoutingService
from app.application.usecase_registry import UseCaseRegistry
from app.config import get_settings
from app.interface.api import dependencies
from app.interface.api.admin import router as admin_router
from app.interface.api.auth import router as auth_router
from app.interface.api.audit_review import router as audit_review_router
from app.interface.api.break_glass import router as break_glass_router
from app.interface.api.patient_rights import rights_router as patient_rights_register_router
from app.interface.api.patient_rights import router as patient_rights_router
from app.interface.api.health import router as health_router
from app.interface.api.landing import router as landing_router
from app.interface.api.jobs import router as jobs_router
from app.interface.api.reports import router as reports_router
from app.interface.api.results import router as results_router
from app.interface.api.studies import orthanc_router, router as studies_router
from app.interface.api.usecases import router as usecases_router
from app.interface.api.critical_alerts import router as critical_alerts_router
from app.interface.api.dicom_upload import router as dicom_upload_router
from app.interface.api.dicomweb import router as dicomweb_router
from app.interface.api.roles import router as roles_router
from app.interface.api.reading import router as reading_router
from app.interface.api.review_signoff import router as review_signoff_router
from app.interface.api.clinical import router as clinical_router
from app.interface.api.onboarding import router as onboarding_router
from app.interface.api.plan_features import router as plan_features_router
from app.interface.api.practitioners import router as practitioners_router
from app.interface.api.mammography import router as mammography_router
from app.interface.api.medgemma_debug import router as medgemma_debug_router
from app.interface.api.tenant_api_keys import router as tenant_api_keys_router
from app.interface.api.tenants import router as tenants_router
from app.interface.api.dashboards import router as dashboards_router
from app.interface.api.platform_admin import router as platform_admin_router
from app.interface.api.tenant_workspace import router as tenant_workspace_router
from app.interface.api.viewer_access import router as viewer_access_router
from app.interface.api.ws import router as ws_router
from app.interface.middleware.auth import RBACMiddleware
from app.interface.middleware.tenant import TenantResolutionMiddleware

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        structlog.get_config().get("min_level", 0)
    ),
)

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("starting_mri_platform")

    from app.infrastructure.database.session import rls_enforcement_status

    settings = get_settings()
    from app.config import insecure_config_problems

    problems = insecure_config_problems(settings)
    if problems and settings.enforce_secure_config:
        raise RuntimeError(
            "Refusing to start with an insecure configuration: " + "; ".join(problems)
            + ". Fix these in .env (ENFORCE_SECURE_CONFIG=false only for local development)."
        )
    if problems:
        logger.warning("insecure_config_allowed", problems=problems)
    from app.config import config_warnings

    for warning in config_warnings(settings):
        logger.warning("config_warning", detail=warning)

    rls_enforced, db_role = await rls_enforcement_status()
    if rls_enforced:
        logger.info("tenant_rls_enforced", db_role=db_role)
    elif settings.require_rls:
        raise RuntimeError(
            f"REQUIRE_RLS is set but the database role {db_role!r} bypasses Row-Level "
            "Security (superuser or BYPASSRLS). Set POSTGRES_APP_USER/POSTGRES_APP_PASSWORD "
            "to the RLS-bound application role created by migration 044."
        )
    else:
        logger.warning(
            "tenant_rls_not_enforced",
            db_role=db_role,
            detail="tenant isolation is application-level only; set POSTGRES_APP_USER",
        )

    registry = UseCaseRegistry()
    await registry.discover_and_register()

    routing_service = RoutingService(registry)

    dependencies.set_registry(registry)
    dependencies.set_routing_service(routing_service)

    # Tenant-addressed realtime events (published by API + Celery worker via Redis),
    # delivered to this process's WebSockets of the matching tenant only.
    import asyncio

    from app.infrastructure.realtime.events import listen_tenant_events
    from app.interface.api.ws import manager as ws_manager

    realtime_task = asyncio.create_task(listen_tenant_events(ws_manager.send_to_tenant))

    logger.info(
        "platform_ready",
        usecases=list(registry.usecases.keys()),
        site=get_settings().site_id,
    )

    yield

    realtime_task.cancel()
    logger.info("shutting_down_mri_platform")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="MRI AI Platform",
        description="Production-grade AI-based MRI analysis platform",
        version="1.0.0",
        lifespan=lifespan,
        # The interactive docs map the whole API surface; only served when enabled.
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    )

    @app.exception_handler(Exception)
    async def _unhandled_error(request, exc):  # pragma: no cover - exercised via tests
        """Never return exception text (paths, SQL, PACS responses) to the client: log it
        with a reference the user can quote to support."""
        import uuid

        from fastapi.responses import JSONResponse

        error_id = uuid.uuid4().hex[:12]
        logger.error("unhandled_error", error_id=error_id, path=request.url.path,
                     error_type=type(exc).__name__, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred", "error_id": error_id},
        )

    origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Starlette executes the LAST-added middleware first, so RBACMiddleware (added
    # last, unchanged position) still runs before TenantResolutionMiddleware, which
    # depends on request.state.tenant_id already being set from the JWT.
    # Runs inside RBAC/tenant resolution (needs the authenticated user on request.state):
    # records every successful read of a patient-data route (HIPAA 164.312(b)).
    from app.interface.middleware.phi_audit import PhiReadAuditMiddleware

    app.add_middleware(PhiReadAuditMiddleware)
    app.add_middleware(TenantResolutionMiddleware)
    app.add_middleware(RBACMiddleware)

    # Routers
    app.include_router(health_router)
    app.include_router(landing_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(studies_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(results_router, prefix="/api")
    app.include_router(usecases_router, prefix="/api")
    app.include_router(admin_router, prefix="/api")
    app.include_router(orthanc_router, prefix="/api")
    app.include_router(reports_router, prefix="/api")
    app.include_router(critical_alerts_router, prefix="/api")
    app.include_router(break_glass_router, prefix="/api")
    app.include_router(audit_review_router, prefix="/api")
    app.include_router(patient_rights_router, prefix="/api")
    app.include_router(patient_rights_register_router, prefix="/api")
    app.include_router(roles_router, prefix="/api")
    app.include_router(reading_router, prefix="/api")
    app.include_router(review_signoff_router, prefix="/api")
    app.include_router(onboarding_router, prefix="/api")
    app.include_router(clinical_router, prefix="/api")
    app.include_router(practitioners_router, prefix="/api")
    app.include_router(mammography_router, prefix="/api")
    if settings.debug_routes_enabled:
        app.include_router(medgemma_debug_router, prefix="/api")
    app.include_router(dicomweb_router, prefix="/api")
    app.include_router(viewer_access_router, prefix="/api")
    app.include_router(dashboards_router, prefix="/api")
    app.include_router(tenant_workspace_router, prefix="/api")
    app.include_router(platform_admin_router, prefix="/api")
    app.include_router(tenants_router, prefix="/api")
    app.include_router(tenant_api_keys_router, prefix="/api")
    app.include_router(plan_features_router, prefix="/api")
    app.include_router(dicom_upload_router, prefix="/api")
    app.include_router(ws_router)

    # Prometheus metrics
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
        Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    except ImportError:
        logger.warning("prometheus_fastapi_instrumentator not installed, /metrics disabled")

    return app


app = create_app()
