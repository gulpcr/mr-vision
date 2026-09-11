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
from app.interface.api.clinical import router as clinical_router
from app.interface.api.onboarding import router as onboarding_router
from app.interface.api.plan_features import router as plan_features_router
from app.interface.api.practitioners import router as practitioners_router
from app.interface.api.mammography import router as mammography_router
from app.interface.api.medgemma_debug import router as medgemma_debug_router
from app.interface.api.tenant_api_keys import router as tenant_api_keys_router
from app.interface.api.tenants import router as tenants_router
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

    registry = UseCaseRegistry()
    await registry.discover_and_register()

    routing_service = RoutingService(registry)

    dependencies.set_registry(registry)
    dependencies.set_routing_service(routing_service)

    logger.info(
        "platform_ready",
        usecases=list(registry.usecases.keys()),
        site=get_settings().site_id,
    )

    yield

    logger.info("shutting_down_mri_platform")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="MRI AI Platform",
        description="Production-grade AI-based MRI analysis platform",
        version="1.0.0",
        lifespan=lifespan,
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
    app.include_router(roles_router, prefix="/api")
    app.include_router(reading_router, prefix="/api")
    app.include_router(onboarding_router, prefix="/api")
    app.include_router(clinical_router, prefix="/api")
    app.include_router(practitioners_router, prefix="/api")
    app.include_router(mammography_router, prefix="/api")
    app.include_router(medgemma_debug_router, prefix="/api")
    app.include_router(dicomweb_router, prefix="/api")
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
