from functools import lru_cache
from typing import Annotated, AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cds_service import ClinicalDecisionService
from app.application.job_orchestrator import JobOrchestrator
from app.application.llm_report_service import LLMReportService
from app.application.longitudinal_service import LongitudinalAnalysisService
from app.application.result_service import ResultService
from app.application.routing_service import RoutingService
from app.application.study_service import StudyService
from app.application.usecase_registry import UseCaseRegistry
from app.config import get_settings
from app.infrastructure.database.repositories import (
    PgAuditRepository,
    PgJobRepository,
    PgResultRepository,
    PgSeriesRepository,
    PgStudyRepository,
    PgUseCaseRegistryRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.dicomweb.client import DICOMwebClient
from app.infrastructure.llm.gemini_client import GeminiClient
from app.infrastructure.orthanc.client import OrthancPACSClient
from app.infrastructure.storage.client import get_artifact_store

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


def get_study_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StudyService:
    return StudyService(
        study_repo=PgStudyRepository(session),
        series_repo=PgSeriesRepository(session),
        audit_repo=PgAuditRepository(session),
        pacs_client=OrthancPACSClient(),
        dicomweb_client=DICOMwebClient(),
    )


def get_job_orchestrator(
    session: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
    routing_service: Annotated[RoutingService, Depends(get_routing_service)],
) -> JobOrchestrator:
    return JobOrchestrator(
        study_repo=PgStudyRepository(session),
        series_repo=PgSeriesRepository(session),
        job_repo=PgJobRepository(session),
        audit_repo=PgAuditRepository(session),
        routing_service=routing_service,
        registry=registry,
    )


def get_result_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResultService:
    return ResultService(
        result_repo=PgResultRepository(session),
        artifact_store=get_artifact_store(),
    )


def get_job_repo(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PgJobRepository:
    return PgJobRepository(session)


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
