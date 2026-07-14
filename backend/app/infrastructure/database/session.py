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


@event.listens_for(Session, "before_flush")
def _apply_tenant_containment(session: Session, flush_context, instances) -> None:
    # Registered on the base Session class so it fires for both the sync Session
    # AsyncSession delegates flush to and the plain sync Session Celery tasks open
    # via sessionmaker(bind=engine) — one hook, both call paths.
    tenant = TenantContextService.get_context_or_null()
    if tenant is None:
        return

    for obj in (*session.new, *session.dirty):
        if hasattr(obj, "tenant_id") and (not obj.tenant_id or obj.tenant_id == "default"):
            obj.tenant_id = tenant.tenant_id


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
