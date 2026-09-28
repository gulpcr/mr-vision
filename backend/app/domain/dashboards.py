"""Dashboard widget catalogue and role-default dashboards (pure data, stdlib only).

A dashboard is a list of widgets on a 12-column grid. Each widget instance is
``{"id", "type", "title", "config", "position": {"x", "y", "w", "h"}}``; ``type`` must be
a key of WIDGET_TYPES and ``config`` follows that type's ``config_schema``. Widget data
is always computed server-side inside the caller's tenant DB scope (and, for the
referring Doctor, their referral scope), so a dashboard can never show another tenant's
numbers — whoever built it.
"""
from __future__ import annotations

from typing import Any

from app.domain.permissions import STUDY_READ

GRID_COLUMNS = 12

_PERIOD = {
    "key": "period", "label": "Period", "type": "select", "default": "30d",
    "options": [
        {"value": "1d", "label": "Today"}, {"value": "7d", "label": "Last 7 days"},
        {"value": "30d", "label": "Last 30 days"}, {"value": "90d", "label": "Last 90 days"},
    ],
}
_LIMIT = {"key": "limit", "label": "Rows", "type": "number", "default": 8, "min": 1, "max": 50}
_MODALITY = {"key": "modality", "label": "Modality (blank = all)", "type": "string", "default": ""}

# type → descriptor. ``required_permission`` gates both adding the widget and its data.
WIDGET_TYPES: dict[str, dict[str, Any]] = {
    "kpi_studies": {
        "label": "Studies received", "category": "metrics",
        "description": "Number of studies received in the period, with the change vs the previous period.",
        "default_size": {"w": 3, "h": 2}, "required_permission": STUDY_READ,
        "config_schema": [_PERIOD, _MODALITY],
    },
    "reading_status": {
        "label": "Reading status", "category": "metrics",
        "description": "Studies by reading status (unread, in progress, reported, signed).",
        "default_size": {"w": 3, "h": 3}, "required_permission": STUDY_READ,
        "config_schema": [_PERIOD],
    },
    "studies_by_modality": {
        "label": "Studies by modality", "category": "charts",
        "description": "Breakdown of studies by modality in the period.",
        "default_size": {"w": 4, "h": 3}, "required_permission": STUDY_READ,
        "config_schema": [_PERIOD],
    },
    "study_volume_trend": {
        "label": "Study volume trend", "category": "charts",
        "description": "Studies received per day.",
        "default_size": {"w": 6, "h": 3}, "required_permission": STUDY_READ,
        "config_schema": [_PERIOD, _MODALITY],
    },
    "my_worklist": {
        "label": "My worklist", "category": "lists", "personal": True,
        "description": "Studies assigned to you that are not signed yet.",
        "default_size": {"w": 6, "h": 4}, "required_permission": "study.claim",
        "config_schema": [_LIMIT],
    },
    "pending_signoff": {
        "label": "Pending sign-off", "category": "lists",
        "description": "Reported studies waiting for sign-off.",
        "default_size": {"w": 6, "h": 4}, "required_permission": "result.approve",
        "config_schema": [_LIMIT],
    },
    "overdue_studies": {
        "label": "Overdue studies", "category": "lists",
        "description": "Unread or in-progress studies older than the turnaround target.",
        "default_size": {"w": 6, "h": 4}, "required_permission": STUDY_READ,
        "config_schema": [
            {"key": "hours", "label": "Older than (hours)", "type": "number", "default": 24, "min": 1, "max": 720},
            _LIMIT,
        ],
    },
    "critical_alerts": {
        "label": "Critical alerts", "category": "metrics",
        "description": "Open critical-finding alerts and the most recent ones.",
        "default_size": {"w": 4, "h": 3}, "required_permission": "alert.view",
        "config_schema": [_LIMIT],
    },
    "turnaround_time": {
        "label": "Turnaround time", "category": "metrics",
        "description": "Median and 90th-percentile time from receipt to report and to sign-off.",
        "default_size": {"w": 4, "h": 3}, "required_permission": STUDY_READ,
        "config_schema": [_PERIOD],
    },
    "radiologist_workload": {
        "label": "Radiologist workload", "category": "team",
        "description": "Studies in progress and signed per radiologist.",
        "default_size": {"w": 6, "h": 4}, "required_permission": "study.view",
        "config_schema": [_PERIOD],
    },
    "ai_jobs": {
        "label": "AI pipeline status", "category": "metrics",
        "description": "AI jobs by status and by use case.",
        "default_size": {"w": 6, "h": 3}, "required_permission": "study.view",
        "config_schema": [_PERIOD],
    },
    "ai_qa_flags": {
        "label": "AI quality flags", "category": "metrics",
        "description": "Share of AI results carrying QA flags, per use case.",
        "default_size": {"w": 6, "h": 3}, "required_permission": "study.view",
        "config_schema": [_PERIOD],
    },
    "todays_intake": {
        "label": "Patient intake", "category": "metrics",
        "description": "Patients registered and orders created in the period.",
        "default_size": {"w": 4, "h": 2}, "required_permission": "patient.onboard",
        "config_schema": [{**_PERIOD, "default": "1d"}],
    },
    "recent_reports": {
        "label": "Recent reports", "category": "lists",
        "description": "Most recently reported or signed studies.",
        "default_size": {"w": 6, "h": 4}, "required_permission": STUDY_READ,
        "config_schema": [_LIMIT],
    },
    "markdown": {
        "label": "Notice", "category": "content",
        "description": "A text notice for the workspace (plain text / simple markdown).",
        "default_size": {"w": 4, "h": 2}, "required_permission": "dashboard.view",
        "config_schema": [{"key": "text", "label": "Text", "type": "text", "default": ""}],
    },
}


def _w(wid: str, wtype: str, x: int, y: int, w: int | None = None, h: int | None = None,
       config: dict | None = None, title: str | None = None) -> dict[str, Any]:
    spec = WIDGET_TYPES[wtype]
    size = spec["default_size"]
    return {
        "id": wid,
        "type": wtype,
        "title": title or spec["label"],
        "config": {f["key"]: f["default"] for f in spec["config_schema"]} | (config or {}),
        "position": {"x": x, "y": y, "w": w or size["w"], "h": h or size["h"]},
    }


# role → default dashboard, seeded into every tenant (editable per tenant by its admins).
ROLE_DEFAULT_DASHBOARDS: dict[str, dict[str, Any]] = {
    "admin": {"name": "Workspace overview", "widgets": [
        _w("kpi", "kpi_studies", 0, 0),
        _w("status", "reading_status", 3, 0),
        _w("tat", "turnaround_time", 6, 0),
        _w("alerts", "critical_alerts", 10, 0, w=2),
        _w("trend", "study_volume_trend", 0, 3),
        _w("workload", "radiologist_workload", 6, 3),
        _w("jobs", "ai_jobs", 0, 7),
        _w("qa", "ai_qa_flags", 6, 7),
    ]},
    "radiologist": {"name": "My reading", "widgets": [
        _w("mine", "my_worklist", 0, 0),
        _w("signoff", "pending_signoff", 6, 0),
        _w("alerts", "critical_alerts", 0, 4),
        _w("tat", "turnaround_time", 4, 4),
        _w("overdue", "overdue_studies", 0, 7, w=8),
    ]},
    "doctor": {"name": "My patients", "widgets": [
        _w("recent", "recent_reports", 0, 0, w=8),
        _w("kpi", "kpi_studies", 8, 0, w=4, title="My referred studies"),
        _w("alerts", "critical_alerts", 8, 2, w=4, h=4),
    ]},
    "technician": {"name": "Acquisition & AI", "widgets": [
        _w("trend", "study_volume_trend", 0, 0),
        _w("modality", "studies_by_modality", 6, 0),
        _w("jobs", "ai_jobs", 0, 3),
        _w("overdue", "overdue_studies", 6, 3),
    ]},
    "receptionist": {"name": "Front desk", "widgets": [
        _w("intake", "todays_intake", 0, 0),
        _w("status", "reading_status", 4, 0),
        _w("trend", "study_volume_trend", 0, 3, w=8, config={"period": "7d"}),
    ]},
    "viewer": {"name": "Overview", "widgets": [
        _w("kpi", "kpi_studies", 0, 0),
        _w("modality", "studies_by_modality", 3, 0),
        _w("trend", "study_volume_trend", 0, 3),
    ]},
}


def validate_widgets(widgets: list[dict[str, Any]]) -> list[str]:
    """Structural validation of a dashboard's widget list; returns error messages."""
    errors: list[str] = []
    seen: set[str] = set()
    if not isinstance(widgets, list) or len(widgets) > 40:
        return ["widgets must be a list of at most 40 widgets"]
    for i, w in enumerate(widgets):
        if not isinstance(w, dict):
            errors.append(f"widget {i}: not an object")
            continue
        wid, wtype = str(w.get("id", "")), w.get("type")
        if not wid or wid in seen:
            errors.append(f"widget {i}: missing or duplicate id")
        seen.add(wid)
        if wtype not in WIDGET_TYPES:
            errors.append(f"widget {wid}: unknown type {wtype!r}")
        pos = w.get("position") or {}
        try:
            x, y, width, height = (int(pos.get(k, -1)) for k in ("x", "y", "w", "h"))
        except (TypeError, ValueError):
            errors.append(f"widget {wid}: invalid position")
            continue
        if not (0 <= x < GRID_COLUMNS and 1 <= width <= GRID_COLUMNS and x + width <= GRID_COLUMNS
                and y >= 0 and 1 <= height <= 12):
            errors.append(f"widget {wid}: position out of the 12-column grid")
        if not isinstance(w.get("config", {}), dict):
            errors.append(f"widget {wid}: config must be an object")
    return errors
