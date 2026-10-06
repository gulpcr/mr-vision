from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.infrastructure.tls import db_async_connect_args, db_sync_url
from app.infrastructure.tenant.context import TenantContextService
from app.infrastructure.tenant.db_scope import apply_scope_sync, current_scope_tenant_id

settings = get_settings()

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    # TLS to Postgres when DB_SSL_MODE is require / verify-full (infrastructure/tls.py).
    connect_args=db_async_connect_args(settings),
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


_sync_session_factory: sessionmaker | None = None


def get_sync_session() -> Session:
    """Sync Session for Celery tasks, from one process-wide engine.

    Celery workers run outside the FastAPI event loop, so they use a separate sync
    engine. It is created once per process (lazily — the API process never needs it)
    instead of once per call, which previously leaked a new connection pool per task.
    """
    global _sync_session_factory
    if _sync_session_factory is None:
        sync_engine = create_engine(db_sync_url(settings.database_url, settings), pool_pre_ping=True)
        _sync_session_factory = sessionmaker(bind=sync_engine)
    return _sync_session_factory()


async def rls_enforcement_status() -> tuple[bool, str]:
    """Whether the app's DB role is actually subject to Row-Level Security.

    Postgres exempts superusers and BYPASSRLS roles from every policy, and table owners
    unless FORCE is set (044 sets it, but superusers bypass even that). Returns
    ``(enforced, role_name)``.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            ))
        ).first()
    if row is None:
        return False, "unknown"
    return (not row.rolsuper and not row.rolbypassrls), row.current_user


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
    tenant_id = tenant.tenant_id if tenant is not None else current_scope_tenant_id()
    if not tenant_id:
        return

    for obj in (*session.new, *session.dirty):
        if hasattr(obj, "tenant_id") and (not obj.tenant_id or obj.tenant_id == "default"):
            obj.tenant_id = tenant_id


@event.listens_for(Session, "after_begin")
def _apply_rls_scope(session: Session, transaction, connection) -> None:
    """SET LOCAL the RLS tenant GUCs at the start of every transaction.

    Registered on the base sync ``Session`` for the same reason as the flush hook above:
    it covers both AsyncSession (which delegates to a sync Session) and the Celery sync
    sessions. The values come from the ContextVar in infrastructure/tenant/db_scope.py;
    being transaction-local, they are discarded on commit/rollback and can never leak to
    the next borrower of a pooled connection.
    """
    apply_scope_sync(connection)
