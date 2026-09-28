"""What a tenant's plan entitles it to: AI use cases and a permission ceiling.

Resolved from ``plans`` (alembic 048) plus the tenant's own additive ``features``
override, and cached briefly per tenant — every authenticated request needs it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select

from app.infrastructure.database.models import PlanRecord, TenantRecord
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.tenant.db_scope import platform_scope

_TTL_SECONDS = 30.0
_cache: dict[str, tuple["TenantEntitlements", float]] = {}


@dataclass(frozen=True)
class TenantEntitlements:
    plan: str
    # None = every registered use case.
    usecases: frozenset[str] | None
    # Permission ceiling; {"*"} = none.
    permissions: frozenset[str]

    def allows_usecase(self, usecase_name: str) -> bool:
        return self.usecases is None or usecase_name in self.usecases


UNRESTRICTED = TenantEntitlements(plan="", usecases=None, permissions=frozenset({"*"}))


def invalidate_entitlements() -> None:
    _cache.clear()


async def get_tenant_entitlements(tenant_id: str) -> TenantEntitlements:
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit and hit[1] > now:
        return hit[0]
    with platform_scope():
        async with async_session_factory() as session:
            row = (await session.execute(
                select(TenantRecord.plan, TenantRecord.features, PlanRecord.usecases, PlanRecord.permissions)
                .join(PlanRecord, PlanRecord.name == TenantRecord.plan, isouter=True)
                .where(TenantRecord.id == tenant_id)
            )).first()
    if row is None:
        result = UNRESTRICTED
    else:
        usecases = None
        if row.usecases is not None:
            usecases = frozenset(row.usecases) | frozenset(row.features or [])
        result = TenantEntitlements(
            plan=row.plan or "",
            usecases=usecases,
            permissions=frozenset(row.permissions or ["*"]),
        )
    _cache[tenant_id] = (result, now + _TTL_SECONDS)
    return result
