"""Tenant provisioning: everything a new workspace needs, created in one transaction.

Creating a tenant used to insert the tenant row and an admin user with a temporary
password — and nothing else: no roles (so every non-admin user of the tenant had no
permissions), no settings, no dashboards. Provisioning now seeds, atomically:

* the system roles (admin, radiologist, doctor, technician, receptionist, viewer);
* the tenant_settings and tenant_branding rows;
* the role-default dashboards;
* optionally a DICOM called AE title for the tenant's scanners and a seat limit;
* the first workspace admin as an **invited** account (one-time link, no password
  ever shown to the operator).

``seed_tenant_defaults`` is idempotent and also backs the migration backfill for
existing tenants.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select

from app.domain.models import AuditEntry, Tenant, User
from app.domain.permissions import SYSTEM_ROLE_PERMISSIONS

logger = structlog.get_logger(__name__)


@dataclass
class ProvisionedTenant:
    tenant: Tenant
    admin_user: User
    invite_token: str
    dicom_endpoint: dict[str, Any] | None


async def seed_tenant_defaults(session, tenant_id: str, display_name: str) -> None:
    """Insert whatever of the per-tenant defaults is missing (roles, settings, branding,
    role-default dashboards). Never overwrites existing rows."""
    from app.application.dashboard_service import seed_default_dashboards
    from app.infrastructure.database.models import (
        RoleRecord,
        TenantBrandingRecord,
        TenantSettingsRecord,
    )

    existing_roles = {
        r[0]
        for r in (
            await session.execute(select(RoleRecord.name).where(RoleRecord.tenant_id == tenant_id))
        ).all()
    }
    for name, perms in SYSTEM_ROLE_PERMISSIONS.items():
        if name not in existing_roles:
            session.add(RoleRecord(
                id=str(uuid.uuid4()), tenant_id=tenant_id, name=name,
                permissions=list(perms), is_system=True,
            ))
    if await session.get(TenantSettingsRecord, tenant_id) is None:
        session.add(TenantSettingsRecord(tenant_id=tenant_id, institution_name=display_name))
    if await session.get(TenantBrandingRecord, tenant_id) is None:
        session.add(TenantBrandingRecord(tenant_id=tenant_id, display_name=display_name))
    await session.flush()
    await seed_default_dashboards(session, tenant_id)


class TenantProvisioningService:
    def __init__(self, session, tenant_service, auth_service):
        self._session = session
        self._tenants = tenant_service
        self._auth = auth_service

    async def provision(
        self,
        *,
        name: str,
        slug: str,
        plan: str,
        features: list[str] | None,
        admin_username: str,
        admin_email: str,
        admin_full_name: str = "",
        called_aet: str | None = None,
        max_users: int | None = None,
        actor: str = "system",
    ) -> ProvisionedTenant:
        from app.application.dicom_endpoint_service import DicomEndpointService
        from app.infrastructure.database.models import TenantRecord
        from app.infrastructure.database.repositories import PgAuditRepository

        tenant = await self._tenants.create_tenant(name=name, slug=slug, plan=plan, features=features)
        if max_users:
            record = await self._session.get(TenantRecord, tenant.id)
            record.max_users = max_users
        await seed_tenant_defaults(self._session, tenant.id, name)

        endpoint = None
        if called_aet:
            endpoint = await DicomEndpointService(self._session).create(
                tenant.id, called_aet, None, "Primary modality endpoint"
            )

        admin_user, token = await self._auth.invite_user(
            tenant_id=tenant.id, username=admin_username, email=admin_email, role="admin",
            full_name=admin_full_name, invited_by=actor,
        )
        await PgAuditRepository(self._session, tenant_id=tenant.id).save(AuditEntry(
            action="tenant_provisioned",
            entity_type="tenant",
            entity_id=tenant.id,
            actor=actor,
            details={"slug": slug, "plan": plan, "called_aet": called_aet, "max_users": max_users},
            tenant_id=tenant.id,
        ))
        logger.info("tenant_provisioned", tenant_id=tenant.id, slug=slug)
        return ProvisionedTenant(tenant=tenant, admin_user=admin_user, invite_token=token,
                                 dicom_endpoint=endpoint)
