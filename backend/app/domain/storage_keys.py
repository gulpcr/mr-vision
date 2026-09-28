"""Object-store key layout for pipeline artifacts.

The artifact bucket is shared by every tenant and MinIO has no notion of tenancy, so the
tenant is encoded in the key itself: ``{tenant_id}/{study_uid}/{usecase}/{name}``. That
makes per-tenant listing, purge and (later) per-tenant bucket policies a prefix
operation, and keeps two tenants' artifacts for the same study UID from colliding.

Objects written before tenant prefixes existed live at the legacy
``{study_uid}/{usecase}/{name}``; readers fall back to that key, and
``backend/scripts/migrate_artifact_keys.py`` moves them under their tenant prefix.
Access control is NOT derived from the key — callers must first check that the study's
result is visible to the requesting tenant (see ResultService).
"""
from __future__ import annotations


def artifact_key(tenant_id: str, study_uid: str, usecase: str, name: str) -> str:
    if not tenant_id:
        raise ValueError("artifact_key requires a tenant_id")
    return f"{tenant_id}/{study_uid}/{usecase}/{name}"


def legacy_artifact_key(study_uid: str, usecase: str, name: str) -> str:
    return f"{study_uid}/{usecase}/{name}"


def study_prefixes(tenant_id: str | None, study_uid: str) -> list[str]:
    """Every prefix a study's artifacts may live under (current layout first)."""
    prefixes = [f"{tenant_id}/{study_uid}/"] if tenant_id else []
    prefixes.append(f"{study_uid}/")
    return prefixes
