from __future__ import annotations

import re

import structlog

from app.domain.interfaces import TenantRepository
from app.domain.models import Tenant

logger = structlog.get_logger(__name__)

VALID_STATUSES = frozenset({"active", "suspended", "offboarded"})

# Mirrors IGNORED_SUBDOMAINS in interface/middleware/tenant.py — those subdomains
# are never resolved to a tenant, so a tenant created with one of these slugs
# (or "default", our seeded system tenant) would be permanently unreachable via
# subdomain routing.
RESERVED_SLUGS = frozenset({"www", "api", "app", "admin", "default", "mail", "ftp", "localhost"})
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,61}[a-z0-9]$")


class TenantService:
    """Tenant workspace lifecycle: create, list, and manage plan/features/status."""

    def __init__(self, tenant_repo: TenantRepository):
        self._tenant_repo = tenant_repo

    async def create_tenant(
        self,
        name: str,
        slug: str,
        plan: str = "starter",
        features: list[str] | None = None,
    ) -> Tenant:
        self._validate_slug(slug)

        existing = await self._tenant_repo.get_by_slug(slug)
        if existing is not None:
            raise ValueError(f"Tenant slug '{slug}' already exists")

        tenant = Tenant(
            id=slug,
            name=name,
            slug=slug,
            is_active=True,
            status="active",
            plan=plan,
            features=features or [],
        )
        return await self._tenant_repo.save(tenant)

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        return await self._tenant_repo.get_by_id(tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        return await self._tenant_repo.get_by_slug(slug)

    async def list_tenants(self) -> list[Tenant]:
        return await self._tenant_repo.list_all()

    async def update_plan(self, tenant_id: str, plan: str) -> Tenant:
        tenant = await self._require_tenant(tenant_id)
        tenant.plan = plan
        return await self._tenant_repo.update(tenant)

    async def update_features(self, tenant_id: str, features: list[str]) -> Tenant:
        tenant = await self._require_tenant(tenant_id)
        tenant.features = features
        return await self._tenant_repo.update(tenant)

    async def set_status(self, tenant_id: str, status: str) -> Tenant:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'; must be one of {sorted(VALID_STATUSES)}")
        tenant = await self._require_tenant(tenant_id)
        tenant.status = status
        tenant.is_active = status == "active"
        updated = await self._tenant_repo.update(tenant)
        logger.info("tenant_status_changed", tenant_id=tenant_id, status=status)
        return updated

    async def _require_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self._tenant_repo.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant '{tenant_id}' not found")
        return tenant

    @staticmethod
    def _validate_slug(slug: str) -> None:
        if slug in RESERVED_SLUGS:
            raise ValueError(f"Slug '{slug}' is reserved and cannot be used for a tenant")
        if not SLUG_PATTERN.match(slug):
            raise ValueError(
                "Slug must be 3-63 characters, lowercase letters/digits/hyphens only, "
                "starting and ending with a letter or digit"
            )
