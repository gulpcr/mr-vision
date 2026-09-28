"""Per-execution-scope database tenant binding for Postgres Row-Level Security.

Every tenant-owned table has an RLS policy (alembic 044) that only exposes rows whose
``tenant_id`` equals the ``app.tenant_id`` GUC, or every row when ``app.platform`` is
``'on'``. This module holds *which* of those two the current request / Celery task is
entitled to, and pushes it into each database transaction:

* ``session.py`` registers an ``after_begin`` listener that calls ``apply_scope_sync``
  on every new transaction, so the GUCs are always ``SET LOCAL`` (transaction-scoped —
  they can never leak to the next user of a pooled connection).
* ``RBACMiddleware`` binds the caller's JWT tenant for every authenticated request.
* Cross-tenant code paths (login lookup, platform-admin endpoints, the Orthanc ingest
  webhook, cross-tenant Celery beat jobs) must opt in explicitly via ``platform_scope()``
  or ``bind_platform_scope()`` — there is no implicit fallback to "see everything".

An unbound scope sets neither GUC, so under RLS the session sees **no** tenant rows.
That fail-closed default is deliberate: a forgotten binding surfaces as empty results
or a WITH CHECK violation, never as another tenant's data.

When the app connects as the table owner / a superuser (``postgres_app_user`` unset —
the pre-RLS deployment mode), Postgres bypasses the policies and these GUCs are inert;
the application-level ``tenant_id`` filters in the repositories remain in force either
way (defence in depth).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

_SET_SCOPE_SQL = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true), "
    "set_config('app.platform', :platform, true), "
    "set_config('app.referring_user', :referring_user, true)"
)


@dataclass(frozen=True)
class DbScope:
    tenant_id: str | None = None
    platform: bool = False
    # Set for referral-scoped users (the referring Doctor, permission
    # ``study.view.referred`` without ``study.view``): the RLS policies from alembic 046
    # then additionally confine studies and every study-derived row to studies whose
    # ``referring_user_id`` is this user.
    referring_user_id: str | None = None


_current_scope: ContextVar[DbScope | None] = ContextVar("db_scope", default=None)


def get_db_scope() -> DbScope | None:
    return _current_scope.get()


def current_scope_tenant_id() -> str | None:
    scope = _current_scope.get()
    return scope.tenant_id if scope else None


def bind_tenant_scope(tenant_id: str, referring_user_id: str | None = None) -> Token:
    if not tenant_id:
        raise ValueError("bind_tenant_scope requires a non-empty tenant_id")
    return _current_scope.set(
        DbScope(tenant_id=tenant_id, platform=False, referring_user_id=referring_user_id or None)
    )


def current_referring_user_id() -> str | None:
    scope = _current_scope.get()
    return scope.referring_user_id if scope else None


def bind_platform_scope(tenant_id: str | None = None) -> Token:
    """Cross-tenant access. ``tenant_id`` (optional) is kept only so row stamping
    (session.py before_flush) still has a default tenant for inserts."""
    return _current_scope.set(DbScope(tenant_id=tenant_id, platform=True))


def clear_scope() -> Token:
    return _current_scope.set(None)


def reset_scope(token: Token) -> None:
    _current_scope.reset(token)


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    token = bind_tenant_scope(tenant_id)
    try:
        yield
    finally:
        reset_scope(token)


@contextmanager
def platform_scope(tenant_id: str | None = None) -> Iterator[None]:
    token = bind_platform_scope(tenant_id)
    try:
        yield
    finally:
        reset_scope(token)


def _scope_params() -> dict[str, str]:
    scope = _current_scope.get()
    if scope is None:
        return {"tenant_id": "", "platform": "off", "referring_user": ""}
    return {
        "tenant_id": scope.tenant_id or "",
        "platform": "on" if scope.platform else "off",
        "referring_user": (scope.referring_user_id or "") if not scope.platform else "",
    }


def apply_scope_sync(connection: Any) -> None:
    """Push the bound scope into the current transaction (sync Connection)."""
    connection.execute(_SET_SCOPE_SQL, _scope_params())


async def reapply_scope(session: Any) -> None:
    """Re-push the scope mid-transaction on an AsyncSession.

    ``after_begin`` only fires when a transaction starts; code that changes the bound
    scope *inside* an already-open transaction (e.g. the ingest webhook resolving the
    study's tenant after a platform-scoped lookup) must call this so the new scope takes
    effect for the remaining statements.
    """
    await session.execute(_SET_SCOPE_SQL, _scope_params())


def reapply_scope_sync(session: Any) -> None:
    """Sync-Session counterpart of ``reapply_scope`` (Celery tasks)."""
    session.execute(_SET_SCOPE_SQL, _scope_params())
