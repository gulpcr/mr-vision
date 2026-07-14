from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.infrastructure.database.models import PlanFeatureRecord, TenantRecord as TenantORMRecord
from app.infrastructure.database.session import async_session_factory


@dataclass(frozen=True)
class TenantLookupResult:
    tenant_id: str
    slug: str
    plan: str
    status: str
    features: list[str] = field(default_factory=list)


async def get_tenant_by_slug(slug: str) -> TenantLookupResult | None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(TenantORMRecord).where(TenantORMRecord.slug == slug)
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None

        plan = record.plan or "starter"
        plan_features_result = await session.execute(
            select(PlanFeatureRecord.feature_key).where(PlanFeatureRecord.plan_name == plan)
        )
        plan_features = {row[0] for row in plan_features_result.all()}

        # Additive resolution: plan defaults UNION the tenant's own overrides —
        # a tenant can be granted extra features beyond its plan, but
        # tenants.features can't revoke a plan-granted one (see migration 029).
        resolved_features = sorted(plan_features | set(record.features or []))

        return TenantLookupResult(
            tenant_id=record.id,
            slug=record.slug,
            plan=plan,
            status=record.status or ("active" if record.is_active else "suspended"),
            features=resolved_features,
        )
