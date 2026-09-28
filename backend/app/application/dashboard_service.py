"""Dashboards: role defaults, personal and shared layouts, version history, widget data.

Visibility (always within the caller's tenant — RLS plus explicit filters):
  * your own dashboards;
  * dashboards shared with the workspace;
  * the role-default dashboard for your role (workspace admins see every role default).

Editing: owners edit their own; workspace admins (``*``) also edit role defaults and
shared dashboards. Role defaults cannot be deleted — clone one to customise it
personally.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import func, or_, select

from app.application.dashboard_widgets import WIDGET_HANDLERS, WidgetContext
from app.domain.dashboards import ROLE_DEFAULT_DASHBOARDS, WIDGET_TYPES, validate_widgets
from app.domain.models import AuditEntry
from app.domain.permissions import has_permission
from app.infrastructure.database.models import DashboardLayoutRecord, DashboardVersionRecord

logger = structlog.get_logger(__name__)

_DATA_TTL = 30.0
_data_cache: dict[str, tuple[dict, float]] = {}
_CACHE_MAX = 5_000
_MAX_VERSIONS = 50


class DashboardNotFound(Exception):
    pass


class DashboardForbidden(Exception):
    pass


class DashboardValidationError(ValueError):
    pass


async def seed_default_dashboards(session, tenant_id: str) -> None:
    """Insert any missing role-default dashboard for ``tenant_id`` (idempotent)."""
    existing = {
        r[0] for r in (await session.execute(
            select(DashboardLayoutRecord.role_default).where(
                DashboardLayoutRecord.tenant_id == tenant_id,
                DashboardLayoutRecord.is_default == True,  # noqa: E712
            )
        )).all()
    }
    for role, spec in ROLE_DEFAULT_DASHBOARDS.items():
        if role not in existing:
            session.add(DashboardLayoutRecord(
                id=str(uuid.uuid4()), tenant_id=tenant_id, name=spec["name"], is_default=True,
                role_default=role, widgets=copy.deepcopy(spec["widgets"]), filters={},
            ))
    await session.flush()


def widget_types_for(permissions) -> list[dict[str, Any]]:
    """The widget catalogue entries the caller may add (and see data for)."""
    return [
        {"type": t, **{k: v for k, v in spec.items() if k != "required_permission"}}
        for t, spec in WIDGET_TYPES.items()
        if has_permission(permissions, spec["required_permission"])
    ]


class DashboardService:
    def __init__(self, session, *, tenant_id: str, user_id: str, role: str | None,
                 permissions, referring_user_id: str | None = None, actor: str = "system"):
        self._session = session
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._role = role
        self._permissions = frozenset(permissions or ())
        self._referring_user_id = referring_user_id
        self._actor = actor

    @property
    def _is_admin(self) -> bool:
        return has_permission(self._permissions, "*")

    # ── visibility / access ────────────────────────────────────────────────
    def _visible_clause(self):
        clauses = [
            DashboardLayoutRecord.owner_id == self._user_id,
            DashboardLayoutRecord.is_shared == True,  # noqa: E712
        ]
        if self._is_admin:
            clauses.append(DashboardLayoutRecord.is_default == True)  # noqa: E712
        elif self._role:
            clauses.append(
                (DashboardLayoutRecord.is_default == True)  # noqa: E712
                & (DashboardLayoutRecord.role_default == self._role)
            )
        return or_(*clauses)

    async def _get_record(self, dashboard_id: str) -> DashboardLayoutRecord:
        record = (await self._session.execute(
            select(DashboardLayoutRecord).where(
                DashboardLayoutRecord.id == dashboard_id,
                DashboardLayoutRecord.tenant_id == self._tenant_id,
                self._visible_clause(),
            )
        )).scalar_one_or_none()
        if record is None:
            raise DashboardNotFound(dashboard_id)
        return record

    def _can_edit(self, record: DashboardLayoutRecord) -> bool:
        if record.owner_id == self._user_id:
            return True
        return self._is_admin and (record.is_default or record.is_shared)

    def _to_dict(self, r: DashboardLayoutRecord) -> dict[str, Any]:
        return {
            "id": r.id, "name": r.name, "description": r.description,
            "owner_id": r.owner_id, "is_default": r.is_default, "role_default": r.role_default,
            "is_shared": r.is_shared, "widgets": r.widgets or [], "filters": r.filters or {},
            "refresh_interval": r.refresh_interval, "can_edit": self._can_edit(r),
            "is_owner": r.owner_id == self._user_id,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }

    # ── CRUD ────────────────────────────────────────────────────────────────
    async def list(self) -> list[dict[str, Any]]:
        rows = (await self._session.execute(
            select(DashboardLayoutRecord)
            .where(DashboardLayoutRecord.tenant_id == self._tenant_id, self._visible_clause())
            .order_by(DashboardLayoutRecord.is_default.desc(), DashboardLayoutRecord.name)
        )).scalars().all()
        items = [self._to_dict(r) for r in rows]
        # The caller's own role default first: the UI opens it by default.
        items.sort(key=lambda d: (not (d["is_default"] and d["role_default"] == self._role),))
        return items

    async def get(self, dashboard_id: str) -> dict[str, Any]:
        return self._to_dict(await self._get_record(dashboard_id))

    def _check_payload(self, payload: dict[str, Any]) -> None:
        if "widgets" in payload:
            errors = validate_widgets(payload["widgets"])
            if errors:
                raise DashboardValidationError("; ".join(errors[:5]))
            forbidden = sorted({
                w["type"] for w in payload["widgets"]
                if not has_permission(self._permissions, WIDGET_TYPES[w["type"]]["required_permission"])
            })
            if forbidden:
                raise DashboardForbidden(f"You cannot use these widgets: {', '.join(forbidden)}")
        if payload.get("is_shared") and not has_permission(self._permissions, "dashboard.share"):
            raise DashboardForbidden("Sharing dashboards requires the dashboard.share permission")
        if "name" in payload and not str(payload["name"]).strip():
            raise DashboardValidationError("name is required")

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_payload(payload)
        record = DashboardLayoutRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id,
            name=str(payload.get("name") or "My dashboard").strip()[:256],
            description=payload.get("description"), owner_id=self._user_id,
            is_default=False, is_shared=bool(payload.get("is_shared", False)),
            widgets=payload.get("widgets") or [], filters=payload.get("filters") or {},
            refresh_interval=int(payload.get("refresh_interval") or 300),
            updated_by=self._actor,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)  # load server-generated timestamps (async-safe)
        await self._audit("dashboard_created", record.id)
        return self._to_dict(record)

    async def update(self, dashboard_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = await self._get_record(dashboard_id)
        if not self._can_edit(record):
            raise DashboardForbidden("You cannot edit this dashboard — clone it instead")
        self._check_payload(payload)
        await self._snapshot(record)
        for field in ("name", "description", "widgets", "filters", "refresh_interval"):
            if field in payload:
                setattr(record, field, payload[field])
        if "is_shared" in payload and not record.is_default:
            record.is_shared = bool(payload["is_shared"])
        record.updated_by = self._actor
        await self._session.flush()
        await self._session.refresh(record)  # onupdate timestamp is server-side
        await self._audit("dashboard_updated", record.id)
        return self._to_dict(record)

    async def delete(self, dashboard_id: str) -> None:
        record = await self._get_record(dashboard_id)
        if record.is_default:
            raise DashboardForbidden("Role-default dashboards cannot be deleted")
        if not self._can_edit(record):
            raise DashboardForbidden("You cannot delete this dashboard")
        await self._session.delete(record)
        await self._session.flush()
        await self._audit("dashboard_deleted", dashboard_id)

    async def clone(self, dashboard_id: str, name: str | None = None) -> dict[str, Any]:
        source = await self._get_record(dashboard_id)
        return await self.create({
            "name": name or f"{source.name} (copy)",
            "description": source.description,
            "widgets": copy.deepcopy(source.widgets or []),
            "filters": copy.deepcopy(source.filters or {}),
            "refresh_interval": source.refresh_interval,
        })

    # ── versions ────────────────────────────────────────────────────────────
    async def _snapshot(self, record: DashboardLayoutRecord) -> None:
        latest = (await self._session.execute(
            select(func.max(DashboardVersionRecord.version_number)).where(
                DashboardVersionRecord.dashboard_id == record.id
            )
        )).scalar_one_or_none() or 0
        self._session.add(DashboardVersionRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id, dashboard_id=record.id,
            version_number=latest + 1, created_by=self._actor,
            snapshot={
                "name": record.name, "description": record.description,
                "widgets": copy.deepcopy(record.widgets or []),
                "filters": copy.deepcopy(record.filters or {}),
                "refresh_interval": record.refresh_interval,
            },
        ))
        if latest + 1 > _MAX_VERSIONS:
            old = (await self._session.execute(
                select(DashboardVersionRecord).where(
                    DashboardVersionRecord.dashboard_id == record.id,
                    DashboardVersionRecord.version_number <= latest + 1 - _MAX_VERSIONS,
                )
            )).scalars().all()
            for row in old:
                await self._session.delete(row)

    async def versions(self, dashboard_id: str) -> list[dict[str, Any]]:
        await self._get_record(dashboard_id)
        rows = (await self._session.execute(
            select(DashboardVersionRecord)
            .where(DashboardVersionRecord.dashboard_id == dashboard_id)
            .order_by(DashboardVersionRecord.version_number.desc())
        )).scalars().all()
        return [{"version": r.version_number, "created_by": r.created_by,
                 "created_at": r.created_at.isoformat() if r.created_at else None,
                 "name": (r.snapshot or {}).get("name"),
                 "widget_count": len((r.snapshot or {}).get("widgets") or [])} for r in rows]

    async def restore(self, dashboard_id: str, version: int) -> dict[str, Any]:
        record = await self._get_record(dashboard_id)
        snap = (await self._session.execute(
            select(DashboardVersionRecord).where(
                DashboardVersionRecord.dashboard_id == dashboard_id,
                DashboardVersionRecord.version_number == version,
            )
        )).scalar_one_or_none()
        if snap is None:
            raise DashboardNotFound(f"{dashboard_id}@{version}")
        return await self.update(record.id, dict(snap.snapshot or {}))

    # ── data ────────────────────────────────────────────────────────────────
    async def data(self, dashboard_id: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Data for every widget of the dashboard; one failing widget never blanks the
        others. Dashboard/viewer filters (period) are overlaid on each widget's config."""
        record = await self._get_record(dashboard_id)
        overlay = {**(record.filters or {}), **(filters or {})}
        ctx = WidgetContext(self._tenant_id, self._user_id, self._referring_user_id)
        out: dict[str, Any] = {}
        for widget in record.widgets or []:
            out[widget.get("id", "")] = await self._widget_data(widget, overlay, ctx)
        return {"data": out}

    async def _widget_data(self, widget: dict[str, Any], overlay: dict[str, Any], ctx: WidgetContext) -> dict:
        wtype = widget.get("type")
        spec = WIDGET_TYPES.get(wtype)
        if spec is None:
            return {"type": wtype, "data": None, "error": "Unknown widget type"}
        if not has_permission(self._permissions, spec["required_permission"]):
            return {"type": wtype, "data": None, "error": "Not permitted"}
        config = {f["key"]: f["default"] for f in spec["config_schema"]}
        config.update(widget.get("config") or {})
        if overlay.get("period"):
            config["period"] = overlay["period"]

        personal = spec.get("personal") or bool(ctx.referring_user_id)
        key_src = json.dumps(config, sort_keys=True, default=str)
        cache_key = ":".join([
            self._tenant_id, wtype, ctx.user_id if personal else "_",
            hashlib.sha1(key_src.encode()).hexdigest()[:12],
        ])
        now = time.monotonic()
        hit = _data_cache.get(cache_key)
        if hit and hit[1] > now:
            return hit[0]
        try:
            data = await WIDGET_HANDLERS[wtype](self._session, ctx, config)
            result = {"type": wtype, "data": data, "fetched_at": time.time()}
        except Exception as exc:  # one broken widget must not blank the dashboard
            logger.warning("dashboard_widget_failed", type=wtype, error=str(exc))
            await self._session.rollback()
            return {"type": wtype, "data": None, "error": "Could not load this widget"}
        if len(_data_cache) >= _CACHE_MAX:
            _data_cache.clear()
        _data_cache[cache_key] = (result, now + _DATA_TTL)
        return result

    async def _audit(self, action: str, dashboard_id: str) -> None:
        from app.infrastructure.database.repositories import PgAuditRepository

        await PgAuditRepository(self._session, tenant_id=self._tenant_id).save(AuditEntry(
            action=action, entity_type="dashboard", entity_id=dashboard_id,
            actor=self._actor, details={}, tenant_id=self._tenant_id,
        ))
