from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.infrastructure.tenant.context import TenantContextService

settings = get_settings()

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@event.listens_for(Session, "before_flush")
def _apply_tenant_containment(session: Session, flush_context, instances) -> None:
    """Backstop tenant-id stamping, not the isolation boundary itself.

    Registered on the base sync ``Session`` class (not ``AsyncSession``) because
    SQLAlchemy's ``AsyncSession`` delegates its actual flush to an underlying sync
    ``Session``, and Celery worker tasks (infrastructure/queue/tasks.py) open their own
    plain sync ``Session`` via ``sessionmaker(bind=engine)`` — one hook covers both the
    FastAPI async request path and the Celery sync task path.

    Reads the contextvars-based TenantContextService (set by TenantResolutionMiddleware
    per-request, or explicitly by a Celery task around a job run — see tasks.py). A
    tenant_id that is already set to something other than the "default" sentinel is never
    clobbered; this only fills in rows that would otherwise fall through to "default".
    """
    tenant = TenantContextService.get_context_or_null()
    if tenant is None:
        return

    for obj in (*session.new, *session.dirty):
        if hasattr(obj, "tenant_id") and (not obj.tenant_id or obj.tenant_id == "default"):
            obj.tenant_id = tenant.tenant_id
