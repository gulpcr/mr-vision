from functools import lru_cache
from typing import Annotated, AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.analytics_service import AnalyticsService
from app.application.auth_service import AuthService
from app.application.cds_service import ClinicalDecisionService
from app.application.dicom_upload_service import DicomUploadService
from app.application.job_orchestrator import JobOrchestrator
from app.application.llm_report_service import LLMReportService
from app.application.longitudinal_service import LongitudinalAnalysisService
from app.application.plan_service import PlanService
from app.application.result_service import ResultService
from app.application.routing_service import RoutingService
from app.application.study_service import StudyService
from app.application.tenant_api_key_service import TenantApiKeyService
from app.application.tenant_service import TenantService
from app.application.usecase_registry import UseCaseRegistry
from app.config import get_settings
from app.infrastructure.database.repositories import (
    PgAuditRepository,
    PgJobRepository,
    PgPendingStudyTenantRepository,
    PgPlanFeatureRepository,
    PgResultRepository,
    PgSeriesRepository,
    PgStudyRepository,
    PgTenantApiKeyRepository,
    PgTenantRepository,
    PgUseCaseRegistryRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.dicomweb.client import DICOMwebClient
from app.infrastructure.llm.gemini_client import GeminiClient
from app.infrastructure.orthanc.client import OrthancPACSClient
from app.infrastructure.storage.client import get_artifact_store
from app.infrastructure.tenant.context import TenantContextService

_registry: UseCaseRegistry | None = None
_routing_service: RoutingService | None = None
_llm_report_service: LLMReportService | None = None
_cds_service: ClinicalDecisionService | None = None
_longitudinal_service: LongitudinalAnalysisService | None = None


def set_registry(registry: UseCaseRegistry):
    global _registry
    _registry = registry


def set_routing_service(service: RoutingService):
    global _routing_service
    _routing_service = service


def get_registry() -> UseCaseRegistry:
    if _registry is None:
        raise RuntimeError("UseCaseRegistry not initialized")
    return _registry


def get_routing_service() -> RoutingService:
    if _routing_service is None:
        raise RuntimeError("RoutingService not initialized")
    return _routing_service


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_db_session():
        yield session


USECASE_FEATURE_LOCKED_DETAIL = "Feature package disabled under your subscription layout"


def request_tenant_id(request: Request) -> str:
    """The authenticated caller's tenant, per the JWT claim RBACMiddleware sets.

    Always populated (jwt/api_key/none auth modes all set "default" at minimum),
    unlike TenantContextService which is only bound when subdomain/header tenant
    resolution is enabled (settings.multi_tenant_enabled) and succeeds. This is the
    row-level isolation boundary — repositories filter every query by it.
    """
    return getattr(request.state, "tenant_id", "default") or "default"


def tenant_has_usecase_access(usecase_name: str) -> bool:
    """Feature-key is the usecase name itself (e.g. "brain_mri", "mammography").
    Single-tenant deployments (multi_tenant_enabled=False) never restrict."""
    if not get_settings().multi_tenant_enabled:
        return True
    return TenantContextService.has_feature(usecase_name)


def require_usecase_feature(usecase_name: str):
    from fastapi import HTTPException

    def _check() -> None:
        if not tenant_has_usecase_access(usecase_name):
            raise HTTPException(status_code=403, detail=USECASE_FEATURE_LOCKED_DETAIL)

    return Depends(_check)


def get_study_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: Annotated[str, Depends(request_tenant_id)],
) -> StudyService:
    return StudyService(
        study_repo=PgStudyRepository(session, tenant_id=tenant_id),
        series_repo=PgSeriesRepository(session, tenant_id=tenant_id),
        audit_repo=PgAuditRepository(session, tenant_id=tenant_id),
        pacs_client=OrthancPACSClient(),
        dicomweb_client=DICOMwebClient(),
        pending_tenant_repo=PgPendingStudyTenantRepository(session),
        unscoped_study_repo=PgStudyRepository(session, tenant_id=None),
    )


def get_job_orchestrator(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: Annotated[str, Depends(request_tenant_id)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
    routing_service: Annotated[RoutingService, Depends(get_routing_service)],
) -> JobOrchestrator:
    return JobOrchestrator(
        study_repo=PgStudyRepository(session, tenant_id=tenant_id),
        series_repo=PgSeriesRepository(session, tenant_id=tenant_id),
        job_repo=PgJobRepository(session, tenant_id=tenant_id),
        audit_repo=PgAuditRepository(session, tenant_id=tenant_id),
        routing_service=routing_service,
        registry=registry,
    )


def get_result_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: Annotated[str, Depends(request_tenant_id)],
) -> ResultService:
    return ResultService(
        result_repo=PgResultRepository(session, tenant_id=tenant_id),
        artifact_store=get_artifact_store(),
    )


def get_job_repo(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: Annotated[str, Depends(request_tenant_id)],
) -> PgJobRepository:
    return PgJobRepository(session, tenant_id=tenant_id)


def get_analytics_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: Annotated[str, Depends(request_tenant_id)],
) -> AnalyticsService:
    return AnalyticsService(session, tenant_id=tenant_id)


def get_tenant_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantService:
    return TenantService(tenant_repo=PgTenantRepository(session))


def get_tenant_api_key_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantApiKeyService:
    return TenantApiKeyService(key_repo=PgTenantApiKeyRepository(session))


def get_plan_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PlanService:
    return PlanService(plan_feature_repo=PgPlanFeatureRepository(session))


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthService:
    return AuthService(session=session)


def get_dicom_upload_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DicomUploadService:
    return DicomUploadService(
        pacs_client=OrthancPACSClient(),
        pending_repo=PgPendingStudyTenantRepository(session),
        unscoped_study_repo=PgStudyRepository(session, tenant_id=None),
        audit_repo=PgAuditRepository(session),
    )


def get_llm_report_service() -> LLMReportService:
    """Singleton — GeminiClient initialises once (SDK configure is idempotent)."""
    global _llm_report_service
    if _llm_report_service is None:
        settings = get_settings()
        client = GeminiClient(
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
        )
        _llm_report_service = LLMReportService(client)
    return _llm_report_service


def get_cds_service() -> ClinicalDecisionService:
    """Singleton — shares the same GeminiClient pattern as the other LLM services."""
    global _cds_service
    if _cds_service is None:
        settings = get_settings()
        client = GeminiClient(
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
        )
        _cds_service = ClinicalDecisionService(client)
    return _cds_service


def get_longitudinal_service() -> LongitudinalAnalysisService:
    """Singleton — LLM longitudinal trend analysis service (Phase 4)."""
    global _longitudinal_service
    if _longitudinal_service is None:
        settings = get_settings()
        client = GeminiClient(
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
        )
        _longitudinal_service = LongitudinalAnalysisService(client)
    return _longitudinal_service
