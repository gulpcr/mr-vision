"""Live principal resolution for authenticated requests.

An access token is valid for hours, but a user's account state can change at any time:
deactivated, role changed, sessions revoked (``token_version`` bumped), their role's
permissions edited. RBACMiddleware therefore resolves every authenticated request to a
``Principal`` read from the database — never from the token's cached ``role`` claim —
with a short in-process cache so this costs at most one query per user per few seconds.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select

from app.infrastructure.database.models import RoleRecord, UserRecord
from app.infrastructure.database.session import async_session_factory
from app.domain.permissions import apply_ceiling
from app.infrastructure.tenant.db_scope import tenant_scope
from app.infrastructure.tenant.entitlements import get_tenant_entitlements

# The seeded Admin tenant (renamed from "default" in alembic 043). Platform-admin and
# platform-operator flags are honoured only for accounts in this tenant, so no tenant's
# own user can ever hold cross-tenant power.
PLATFORM_TENANT_ID = "default"

_TTL_SECONDS = 10.0
_CACHE_MAX = 10_000
_cache: dict[tuple[str, str], tuple["Principal | None", float]] = {}


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    tenant_id: str
    role: str
    permissions: frozenset[str]
    token_version: int
    is_platform_admin: bool
    is_platform_operator: bool
    # AI use cases the tenant's plan includes (None = all).
    allowed_usecases: frozenset[str] | None = None


def invalidate_principal(user_id: str) -> None:
    """Drop cached state for a user (after a role change, deactivation, revocation)."""
    for key in [k for k in _cache if k[0] == user_id]:
        _cache.pop(key, None)


def invalidate_all_principals() -> None:
    """Drop every cached principal (after a role's permissions change)."""
    _cache.clear()


async def load_principal(user_id: str, tenant_id: str) -> Principal | None:
    """The active user ``user_id`` of ``tenant_id`` with their role's live permissions,
    or None if the account no longer exists / is inactive / belongs elsewhere."""
    key = (user_id, tenant_id)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[1] > now:
        return hit[0]

    principal: Principal | None = None
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            user = (
                await session.execute(
                    select(UserRecord).where(
                        UserRecord.id == user_id, UserRecord.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if user is not None and user.is_active and (user.status or "active") == "active":
                role = (
                    await session.execute(
                        select(RoleRecord).where(
                            RoleRecord.tenant_id == tenant_id, RoleRecord.name == user.role
                        )
                    )
                ).scalar_one_or_none()
                in_platform_tenant = tenant_id == PLATFORM_TENANT_ID
                role_permissions = frozenset((role.permissions or []) if role else [])
                principal = Principal(
                    user_id=user.id,
                    username=user.username,
                    tenant_id=tenant_id,
                    role=user.role,
                    permissions=role_permissions,
                    token_version=user.token_version or 0,
                    is_platform_admin=bool(user.is_platform_admin) and in_platform_tenant,
                    is_platform_operator=bool(user.is_platform_operator) and in_platform_tenant,
                )

    if principal is not None:
        # The tenant's plan caps what any of its roles may do, and which AI use cases
        # it can run (plans, alembic 048).
        entitlements = await get_tenant_entitlements(tenant_id)
        principal = Principal(
            **{**principal.__dict__,
               "permissions": apply_ceiling(principal.permissions, entitlements.permissions),
               "allowed_usecases": entitlements.usecases},
        )

    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (principal, now + _TTL_SECONDS)
    return principal
