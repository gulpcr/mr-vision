"""RBAC permission catalog and system-role definitions.

Pure domain module (stdlib only) — the single source of truth for the platform's
permission keys and the seeded system roles. Used by the roles migration (seed),
the RoleService (subset validation), and the require_permission dependency.
"""
from __future__ import annotations

# ── Permission catalog ────────────────────────────────────────────────────────
# key → human description (shown in the roles editor).
PERMISSIONS: dict[str, str] = {
    "study.view": "View the worklist, studies, results, and reports",
    "study.upload": "Upload / ingest DICOM studies",
    "study.claim": "Claim, assign, or reassign studies for reading",
    "study.delete": "Delete a study from the platform",
    "job.run": "Run AI pipelines on a study",
    "job.manage": "Cancel or retry AI jobs",
    "result.approve": "Approve / review AI results",
    "result.export": "Export reports (PDF / DICOM-SR / FHIR) and share links",
    "alert.view": "View critical-finding alerts",
    "alert.acknowledge": "Acknowledge critical-finding alerts",
    "patient.onboard": "Create patients and orders (intake)",
    "user.manage": "Manage users and roles",
    "config.manage": "Manage use cases, routing, sites, retention, experiments",
    "audit.view": "View the audit log",
    "data.purge": "Destructive data operations (reset / retention purge)",
}

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS)

# ── Seeded system roles (per the agreed duties) ────────────────────────────────
#   admin        — full access
#   receptionist — patient onboarding (takes the patient's clinical data)
#   technician   — uploads DICOM studies (and runs/manages the AI on them)
#   radiologist  — approves / exports results, handles critical alerts
#   viewer       — the referring doctor: read-only
SYSTEM_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": sorted(ALL_PERMISSIONS),
    "receptionist": ["patient.onboard", "study.view"],
    "technician": ["job.manage", "job.run", "study.upload", "study.view"],
    "radiologist": [
        "alert.acknowledge",
        "alert.view",
        "result.approve",
        "result.export",
        "study.claim",
        "study.view",
    ],
    "viewer": ["study.view"],
}

SYSTEM_ROLE_NAMES: frozenset[str] = frozenset(SYSTEM_ROLE_PERMISSIONS)


def validate_permissions(perms: list[str]) -> list[str]:
    """Return the list of permission keys in ``perms`` that are NOT recognised."""
    return [p for p in perms if p not in ALL_PERMISSIONS]
