"""Subscription plans (alembic 048): defined by the platform admin, assigned to tenants.

A plan sets, for every tenant on it:
  * ``usecases``     — the AI use cases the tenant may run (None = all registered);
  * ``permissions``  — the permission ceiling for every role in the tenant (["*"] = none);
  * ``default_max_users`` — the seat limit a new tenant on the plan starts with.

Tenants reference plans by ``name`` (the key); ``display_name`` is what people see.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select

from app.domain.permissions import PERMISSIONS, WILDCARD, validate_permissions
from app.infrastructure.database.models import PlanRecord, TenantRecord

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class PlanError(ValueError):
    pass


class PlanNotFound(PlanError):
    pass


class PlanService:
    def __init__(self, session, registered_usecases: list[str] | None = None):
        self._session = session
        self._registered = sorted(registered_usecases or [])

    # ── reads ──────────────────────────────────────────────────────────────
    def catalog(self) -> dict[str, Any]:
        """What a plan can grant: every registered use case and every permission."""
        return {
            "usecases": self._registered,
            "permissions": [{"key": k, "description": v} for k, v in PERMISSIONS.items()],
        }

    async def list_plans(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        stmt = select(PlanRecord).order_by(PlanRecord.display_name)
        if not include_inactive:
            stmt = stmt.where(PlanRecord.is_active == True)  # noqa: E712
        plans = (await self._session.execute(stmt)).scalars().all()
        counts = dict((await self._session.execute(
            select(TenantRecord.plan, func.count()).group_by(TenantRecord.plan)
        )).all())
        return [self._to_dict(p, counts.get(p.name, 0)) for p in plans]

    async def get_plan(self, name: str) -> PlanRecord:
        plan = await self._session.get(PlanRecord, name)
        if plan is None:
            raise PlanNotFound(f"Plan '{name}' does not exist")
        return plan

    # ── writes ─────────────────────────────────────────────────────────────
    def _validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "display_name" in payload:
            display = str(payload["display_name"] or "").strip()
            if not display:
                raise PlanError("display_name is required")
            out["display_name"] = display[:128]
        if "description" in payload:
            out["description"] = (str(payload["description"]).strip() or None) if payload["description"] else None
        if "default_max_users" in payload:
            value = payload["default_max_users"]
            if value is not None and (not isinstance(value, int) or value < 1):
                raise PlanError("default_max_users must be a positive number or empty")
            out["default_max_users"] = value
        if "usecases" in payload:
            usecases = payload["usecases"]
            if usecases is not None:
                unknown = sorted(set(usecases) - set(self._registered)) if self._registered else []
                if unknown:
                    raise PlanError(f"Unknown use case(s): {', '.join(unknown)}")
                usecases = sorted(set(usecases))
            out["usecases"] = usecases
        if "permissions" in payload:
            perms = sorted(set(payload["permissions"] or []))
            if not perms:
                raise PlanError("A plan must allow at least one permission")
            invalid = validate_permissions(perms)
            if invalid:
                raise PlanError(f"Unknown permission(s): {', '.join(invalid)}")
            out["permissions"] = [WILDCARD] if WILDCARD in perms else perms
        if "is_active" in payload:
            out["is_active"] = bool(payload["is_active"])
        return out

    async def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip().lower()
        if not _NAME_RE.match(name):
            raise PlanError("Plan key must be 2–32 characters: lowercase letters, digits, - or _")
        if await self._session.get(PlanRecord, name) is not None:
            raise PlanError(f"Plan '{name}' already exists")
        fields = self._validate({
            "display_name": payload.get("display_name") or name,
            "description": payload.get("description"),
            "default_max_users": payload.get("default_max_users"),
            "usecases": payload.get("usecases"),
            "permissions": payload.get("permissions") or [WILDCARD],
            "is_active": payload.get("is_active", True),
        })
        plan = PlanRecord(name=name, **fields)
        self._session.add(plan)
        await self._session.flush()
        await self._session.refresh(plan)
        return self._to_dict(plan, 0)

    async def update_plan(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        plan = await self.get_plan(name)
        for key, value in self._validate(payload).items():
            setattr(plan, key, value)
        await self._session.flush()
        await self._session.refresh(plan)
        count = (await self._session.execute(
            select(func.count()).select_from(TenantRecord).where(TenantRecord.plan == name)
        )).scalar_one()
        return self._to_dict(plan, count)

    async def delete_plan(self, name: str) -> None:
        plan = await self.get_plan(name)
        in_use = (await self._session.execute(
            select(func.count()).select_from(TenantRecord).where(TenantRecord.plan == name)
        )).scalar_one()
        if in_use:
            raise PlanError(f"{in_use} tenant(s) are on this plan — move them to another plan first")
        await self._session.delete(plan)
        await self._session.flush()

    @staticmethod
    def _to_dict(p: PlanRecord, tenant_count: int) -> dict[str, Any]:
        return {
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "default_max_users": p.default_max_users,
            "usecases": p.usecases,
            "permissions": p.permissions or [WILDCARD],
            "is_active": p.is_active,
            "tenant_count": tenant_count,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
