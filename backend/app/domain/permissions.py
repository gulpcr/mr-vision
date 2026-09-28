"""RBAC permission catalog and system-role definitions.

Pure domain module (stdlib only) — the single source of truth for the platform's
permission keys and the system roles seeded into every tenant. Used by the roles
migrations (seed / backfill), TenantProvisioningService, RoleService (validation), the
RBAC middleware (grant resolution) and require_permission.

Key grammar — ``resource.action[.scope]``:

* A grant of ``*`` satisfies everything; ``resource.*`` satisfies every key under
  ``resource.``; a grant of ``study.view`` also satisfies the narrower
  ``study.view.referred`` (a broader grant always implies its scoped variants).
* A *required* key may list alternatives with ``||`` — ``study.view||study.view.referred``
  passes with either.
* Scoped variants restrict *rows*, not endpoints: a user holding only
  ``study.view.referred`` (the referring Doctor) passes the same study/report endpoints
  as ``study.view``, but the DB scope then limits every study-derived row to studies
  referred by that user (see infrastructure/tenant/db_scope.py and alembic 046).
"""
from __future__ import annotations

# ── Permission catalog ────────────────────────────────────────────────────────
# key → human description (shown in the role builder).
PERMISSIONS: dict[str, str] = {
    "study.view": "View the worklist, studies, results and reports for the whole workspace",
    "study.view.referred": "View only studies, results and reports of patients you referred",
    "study.upload": "Upload / ingest DICOM studies",
    "study.claim": "Claim, assign, or reassign studies for reading",
    "study.escalate": "Escalate a study to an available radiologist for reading (auto-assign only)",
    "study.delete": "Delete a study from the platform",
    "job.run": "Run AI pipelines on a study",
    "job.manage": "Cancel or retry AI jobs",
    "result.approve": "Review AI results, report and sign studies",
    "result.export": "Download / export reports (PDF, DICOM-SR, FHIR) and share links",
    "alert.view": "View critical-finding alerts",
    "alert.acknowledge": "Acknowledge critical-finding alerts",
    "patient.onboard": "Register patients and create orders (intake)",
    "dashboard.view": "View dashboards",
    "dashboard.manage": "Create and edit your own dashboards",
    "dashboard.share": "Share dashboards with the whole workspace",
    "user.manage": "Invite users, change their roles, deactivate them",
    "role.manage": "Create and edit custom roles",
    "settings.manage": "Edit workspace settings and branding",
    "config.manage": "Manage AI use cases, routing, alerts and experiments",
    "audit.view": "View the audit log and QA / capacity metrics",
    "data.purge": "Destructive data operations (reset / retention purge)",
}

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS)
WILDCARD = "*"

# ── System roles seeded into every tenant ─────────────────────────────────────
#   admin        — workspace (tenant) administrator: everything in the workspace
#   radiologist  — reads, reports and signs; handles critical alerts
#   doctor       — referring physician: only their own referred patients' studies/reports
#   technician   — uploads DICOM studies, runs/manages the AI on them
#   receptionist — patient registration and orders
#   viewer       — read-only
SYSTEM_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": [WILDCARD],
    "radiologist": [
        "alert.acknowledge",
        "alert.view",
        "dashboard.manage",
        "dashboard.view",
        "job.run",
        "result.approve",
        "result.export",
        "study.claim",
        "study.escalate",
        "study.view",
    ],
    "doctor": [
        "alert.view",
        "dashboard.manage",
        "dashboard.view",
        "result.export",
        "study.view.referred",
    ],
    "technician": [
        "dashboard.manage",
        "dashboard.view",
        "job.manage",
        "job.run",
        "study.escalate",
        "study.upload",
        "study.view",
    ],
    "receptionist": [
        "dashboard.manage",
        "dashboard.view",
        "patient.onboard",
        "study.view",
    ],
    "viewer": ["dashboard.view", "study.view"],
}

SYSTEM_ROLE_NAMES: frozenset[str] = frozenset(SYSTEM_ROLE_PERMISSIONS)

SYSTEM_ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Workspace administrator",
    "radiologist": "Reads, reports and signs studies",
    "doctor": "Referring physician — own patients only",
    "technician": "Uploads studies and runs AI",
    "receptionist": "Patient registration and orders",
    "viewer": "Read-only access",
}

# Endpoints that read study-derived data accept either the workspace-wide or the
# referral-scoped grant; the DB scope narrows rows for the latter.
STUDY_READ = "study.view||study.view.referred"


def validate_permissions(perms: list[str]) -> list[str]:
    """Return the entries of ``perms`` that are not valid grants."""
    invalid = []
    for p in perms:
        if p == WILDCARD or p in ALL_PERMISSIONS:
            continue
        if p.endswith(".*") and any(k.startswith(p[:-1]) for k in ALL_PERMISSIONS):
            continue
        invalid.append(p)
    return invalid


def _grant_satisfies(grant: str, required: str) -> bool:
    if grant == WILDCARD or grant == required:
        return True
    if grant.endswith(".*"):
        return required.startswith(grant[:-1])
    # A broader grant implies its scoped variants (study.view ⊇ study.view.referred).
    return required.startswith(grant + ".")


def has_permission(granted, required: str) -> bool:
    """Whether the grants in ``granted`` satisfy ``required`` (``||`` = alternatives)."""
    for alternative in required.split("||"):
        alternative = alternative.strip()
        if alternative and any(_grant_satisfies(g, alternative) for g in granted):
            return True
    return False


def is_referral_scoped(granted) -> bool:
    """True when the user may see studies only through a scoped grant (the referring
    Doctor): they hold ``study.view.referred`` but not the workspace-wide ``study.view``."""
    return has_permission(granted, "study.view.referred") and not has_permission(
        granted, "study.view"
    )
