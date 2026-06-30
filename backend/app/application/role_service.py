"""Role management service (RBAC).

Per-tenant CRUD over RoleRecord with subset validation, system-role and in-use
delete protection, and audit on every mutation. Application layer — no FastAPI
imports; the router maps these exceptions to HTTP status codes.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select

from app.domain.permissions import SYSTEM_ROLE_NAMES, validate_permissions

logger = structlog.get_logger(__name__)


class UnknownPermissionError(ValueError):
    """Raised when a role references permission keys not in the catalog (→ 422)."""

    def __init__(self, unknown: list[str]):
        self.unknown = unknown
        super().__init__(f"Unknown permission(s): {', '.join(unknown)}")


class RoleProtectedError(Exception):
    """Raised when attempting to delete a system role (→ 409)."""


class RoleInUseError(Exception):
    """Raised when attempting to delete a role still assigned to users (→ 409)."""


class RoleService:
    def __init__(self, session):
        self._session = session

    async def list_roles(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        from app.infrastructure.database.models import RoleRecord

        res = await self._session.execute(
            select(RoleRecord).where(RoleRecord.tenant_id == tenant_id).order_by(RoleRecord.name)
        )
        return [self._to_dict(r) for r in res.scalars().all()]

    async def create_role(
        self, name: str, permissions: list[str], tenant_id: str = "default", actor: str = "system"
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import RoleRecord

        unknown = validate_permissions(permissions)
        if unknown:
            raise UnknownPermissionError(unknown)

        role = RoleRecord(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            name=name,
            permissions=sorted(set(permissions)),
            is_system=False,
        )
        self._session.add(role)
        await self._session.flush()
        await self._audit(actor, "role_created", role.id, {"name": name, "permissions": role.permissions})
        return self._to_dict(role)

    async def update_role(
        self,
        role_id: str,
        tenant_id: str = "default",
        name: str | None = None,
        permissions: list[str] | None = None,
        actor: str = "system",
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import RoleRecord

        role = await self._get(role_id, tenant_id)
        if not role:
            return None

        before = {"name": role.name, "permissions": list(role.permissions or [])}
        if permissions is not None:
            unknown = validate_permissions(permissions)
            if unknown:
                raise UnknownPermissionError(unknown)
            role.permissions = sorted(set(permissions))
        if name is not None:
            role.name = name
        await self._session.flush()
        await self._audit(
            actor, "role_updated", role.id,
            {"before": before, "after": {"name": role.name, "permissions": role.permissions}},
        )
        return self._to_dict(role)

    async def delete_role(
        self, role_id: str, tenant_id: str = "default", actor: str = "system"
    ) -> bool:
        from app.infrastructure.database.models import RoleRecord, UserRecord

        role = await self._get(role_id, tenant_id)
        if not role:
            return False
        if role.is_system or role.name in SYSTEM_ROLE_NAMES:
            raise RoleProtectedError("System roles cannot be deleted")

        in_use = await self._session.execute(
            select(func.count())
            .select_from(UserRecord)
            .where(UserRecord.tenant_id == tenant_id, UserRecord.role == role.name)
        )
        if (in_use.scalar() or 0) > 0:
            raise RoleInUseError("Role is assigned to users — reassign users first")

        await self._audit(actor, "role_deleted", role.id, {"name": role.name})
        await self._session.delete(role)
        await self._session.flush()
        return True

    async def _get(self, role_id: str, tenant_id: str):
        from app.infrastructure.database.models import RoleRecord

        res = await self._session.execute(
            select(RoleRecord).where(RoleRecord.id == role_id, RoleRecord.tenant_id == tenant_id)
        )
        return res.scalar_one_or_none()

    async def _audit(self, actor: str, action: str, entity_id: str, details: dict) -> None:
        from app.infrastructure.database.models import AuditLogRecord

        self._session.add(AuditLogRecord(
            id=str(uuid.uuid4()),
            action=action,
            entity_type="role",
            entity_id=entity_id,
            actor=actor or "system",
            details=details,
        ))
        await self._session.flush()

    @staticmethod
    def _to_dict(r) -> dict[str, Any]:
        return {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "name": r.name,
            "permissions": list(r.permissions or []),
            "is_system": bool(r.is_system),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
