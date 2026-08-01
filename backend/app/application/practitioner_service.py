"""PractitionerService — the write path for Practitioner / PractitionerRole / Organization.

Migration 032 added the columns; this is what fills them. Without it the schema would be
exactly the thing rejected earlier in this work — a table nothing can write to — so the
API surface is part of the deliverable, not an optional extra.

Three concerns, deliberately kept together because they are one administrative screen:

* **Practitioner** — the professional identifier (NPI / licence) and structured HumanName
  on ``users``. A login account is not a clinical actor; the identifier is what
  distinguishes one clinician from another when a report leaves the platform.
* **PractitionerRole** — ``user_roles``, so a person can hold several roles across
  organizations with a validity ``period``. ``users.role`` stays the denormalised primary
  role, because every RBAC check reads it and none of them should have to change.
* **Organization** — identity on ``tenants`` so exported identifiers have an assigner and
  ``DiagnosticReport.performer`` has something real to point at.

Role *permissions* remain RoleService's job; this service only says who holds which role.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import utcnow
from app.infrastructure.database.models import (
    RoleRecord,
    TenantRecord,
    UserRecord,
    UserRoleRecord,
)

logger = structlog.get_logger(__name__)


class PractitionerValidationError(ValueError):
    """Invalid practitioner/organization input (→ 422)."""


class PractitionerService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Practitioner ──────────────────────────────────────────────────────────

    async def update_practitioner(
        self, user_id: str, payload: dict[str, Any], tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        """Set the professional identifier and structured name on a user."""
        user = (await self._session.execute(
            select(UserRecord).where(
                UserRecord.id == user_id, UserRecord.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if user is None:
            return None

        if "identifier_value" in payload:
            value = (payload.get("identifier_value") or "").strip() or None
            system = (payload.get("identifier_system") or "").strip() or None
            if value and not system:
                # Same rule as every other identifier in the platform: a value with no
                # system cannot be matched by a receiving system, so it is not accepted.
                raise PractitionerValidationError(
                    "identifier_system is required when identifier_value is set"
                )
            user.identifier_value = value
            user.identifier_system = system if value else None

        for field in ("family_name", "name_prefix", "name_suffix"):
            if field in payload:
                setattr(user, field, (str(payload[field]).strip() or None)
                        if payload[field] is not None else None)

        if "given_names" in payload:
            given = payload.get("given_names")
            if given is None:
                user.given_names = None
            elif isinstance(given, str):
                user.given_names = [p for p in given.split() if p] or None
            elif isinstance(given, list):
                user.given_names = [str(p).strip() for p in given if str(p).strip()] or None
            else:
                raise PractitionerValidationError("given_names must be a string or a list")

        await self._session.flush()
        return self._practitioner_dict(user, roles=await self._roles_for(user_id))

    async def get_practitioner(
        self, user_id: str, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        user = (await self._session.execute(
            select(UserRecord).where(
                UserRecord.id == user_id, UserRecord.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if user is None:
            return None
        return self._practitioner_dict(user, roles=await self._roles_for(user_id))

    # ── PractitionerRole ──────────────────────────────────────────────────────

    async def add_role(
        self,
        user_id: str,
        role_name: str,
        tenant_id: str = "default",
        role_code_system: str | None = None,
        role_code: str | None = None,
        specialty_code_system: str | None = None,
        specialty_code: str | None = None,
        make_primary: bool = False,
    ) -> dict[str, Any]:
        """Grant a role to a user. Idempotent — re-granting reopens a closed period."""
        user = (await self._session.execute(
            select(UserRecord).where(
                UserRecord.id == user_id, UserRecord.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if user is None:
            raise PractitionerValidationError("user not found")

        name = (role_name or "").strip()
        if not name:
            raise PractitionerValidationError("role_name is required")
        known = (await self._session.execute(
            select(RoleRecord.name).where(
                RoleRecord.name == name, RoleRecord.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if known is None:
            raise PractitionerValidationError(
                f"unknown role {name!r} for this tenant — create it via /api/roles first"
            )

        existing = (await self._session.execute(
            select(UserRoleRecord).where(
                UserRoleRecord.user_id == user_id, UserRoleRecord.role_name == name
            )
        )).scalar_one_or_none()
        if existing is not None:
            # Reopen rather than insert: the unique (user_id, role_name) index means a
            # second row is impossible, and re-granting a revoked role is a real action.
            existing.period_end = None
            row = existing
        else:
            row = UserRoleRecord(
                id=str(uuid.uuid4()),
                user_id=user_id,
                role_name=name,
                organization_id=user.tenant_id,
                tenant_id=tenant_id,
            )
            self._session.add(row)

        if role_code_system and role_code:
            row.role_code_system = role_code_system.strip()
            row.role_code = role_code.strip()
        if specialty_code_system and specialty_code:
            row.specialty_code_system = specialty_code_system.strip()
            row.specialty_code = specialty_code.strip()

        if make_primary:
            await self._set_primary(user, name)

        await self._session.flush()
        return self._role_dict(row)

    async def revoke_role(
        self, user_id: str, role_name: str, tenant_id: str = "default"
    ) -> bool:
        """End a role's period rather than deleting it.

        PractitionerRole.period exists so a historical report can be read against the role
        its signer held at the time; deleting the row would erase that.
        """
        row = (await self._session.execute(
            select(UserRoleRecord).where(
                UserRoleRecord.user_id == user_id,
                UserRoleRecord.role_name == (role_name or "").strip(),
                UserRoleRecord.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is None:
            return False
        if row.is_primary:
            raise PractitionerValidationError(
                "cannot revoke the primary role — assign a different primary first"
            )
        row.period_end = utcnow()
        await self._session.flush()
        return True

    async def _set_primary(self, user: UserRecord, role_name: str) -> None:
        rows = (await self._session.execute(
            select(UserRoleRecord).where(UserRoleRecord.user_id == user.id)
        )).scalars().all()
        for r in rows:
            r.is_primary = r.role_name == role_name
        # Keep the denormalised cache in step, since require_permission reads users.role.
        user.role = role_name

    async def _roles_for(self, user_id: str) -> list[dict[str, Any]]:
        rows = (await self._session.execute(
            select(UserRoleRecord)
            .where(UserRoleRecord.user_id == user_id)
            .order_by(UserRoleRecord.period_start)
        )).scalars().all()
        return [self._role_dict(r) for r in rows]

    # ── Organization ──────────────────────────────────────────────────────────

    async def update_organization(
        self, tenant_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        tenant = (await self._session.execute(
            select(TenantRecord).where(TenantRecord.id == tenant_id)
        )).scalar_one_or_none()
        if tenant is None:
            return None

        if "identifier_value" in payload:
            value = (payload.get("identifier_value") or "").strip() or None
            system = (payload.get("identifier_system") or "").strip() or None
            if value and not system:
                raise PractitionerValidationError(
                    "identifier_system is required when identifier_value is set"
                )
            tenant.identifier_value = value
            tenant.identifier_system = system if value else None

        if "org_type" in payload:
            tenant.org_type = (str(payload["org_type"]).strip() or None) \
                if payload["org_type"] is not None else None
        if "address" in payload:
            address = payload.get("address")
            if address is not None and not isinstance(address, dict):
                # FHIR Address is structured (line[], city, postalCode, country); accepting
                # a flat string here would repeat the mistake made with patient names.
                raise PractitionerValidationError(
                    "address must be a FHIR Address object, not a string"
                )
            tenant.address = address

        await self._session.flush()
        return self._organization_dict(tenant)

    async def get_organization(self, tenant_id: str) -> dict[str, Any] | None:
        tenant = (await self._session.execute(
            select(TenantRecord).where(TenantRecord.id == tenant_id)
        )).scalar_one_or_none()
        return self._organization_dict(tenant) if tenant else None

    # ── Serialisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _practitioner_dict(u: UserRecord, roles: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "full_name": u.full_name,
            "identifier_system": u.identifier_system,
            "identifier_value": u.identifier_value,
            "family_name": u.family_name,
            "given_names": u.given_names,
            "name_prefix": u.name_prefix,
            "name_suffix": u.name_suffix,
            "primary_role": u.role,
            "is_active": u.is_active,
            "roles": roles,
        }

    @staticmethod
    def _role_dict(r: UserRoleRecord) -> dict[str, Any]:
        return {
            "id": r.id,
            "role_name": r.role_name,
            "organization_id": r.organization_id,
            "role_code_system": r.role_code_system,
            "role_code": r.role_code,
            "specialty_code_system": r.specialty_code_system,
            "specialty_code": r.specialty_code,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "is_primary": bool(r.is_primary),
            "active": r.period_end is None,
        }

    @staticmethod
    def _organization_dict(t: TenantRecord) -> dict[str, Any]:
        return {
            "id": t.id,
            "name": t.name,
            "slug": t.slug,
            "identifier_system": t.identifier_system,
            "identifier_value": t.identifier_value,
            "org_type": t.org_type,
            "address": t.address,
            "is_active": t.is_active,
        }
