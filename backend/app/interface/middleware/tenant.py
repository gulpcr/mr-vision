from __future__ import annotations

import ipaddress
import time

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from app.config import get_settings
from app.infrastructure.tenant.context import TenantContext, TenantContextService
from app.infrastructure.tenant.repository import get_tenant_by_id, get_tenant_by_slug

logger = structlog.get_logger(__name__)

# Paths that never require tenant resolution.
PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/metrics", "/api/cortex"})
# Login/register happen before a tenant is known — the username is globally
# unique across `users`, so auth doesn't need a resolved tenant; the user's own
# tenant_id (on their JWT) is the source of truth from that point on. Mirrors
# RBACMiddleware's own AUTH_PATHS exemption.
AUTH_PATHS = frozenset({"/api/auth/login", "/api/auth/register"})
# Resolves its own tenant from a per-tenant API key rather than a JWT/subdomain
# — must bypass this middleware's slug resolution entirely (mirrors RBACMiddleware's
# SELF_AUTHENTICATING_PATHS exemption).
SELF_AUTHENTICATING_PATHS = frozenset({"/api/dicom/upload"})
# The platform-admin surface (including tenant management itself) is not
# scoped to a single tenant — it must be reachable without a resolved tenant.
ADMIN_PATH_PREFIX = "/api/admin"
IGNORED_SUBDOMAINS = frozenset({"www", "api", "app", "admin"})


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _extract_subdomain(host: str, root_domain: str) -> str | None:
    if not host or _is_ip_address(host):
        return None
    root_labels = root_domain.lower().split(".")
    labels = host.lower().split(".")
    if len(labels) <= len(root_labels) or labels[-len(root_labels):] != root_labels:
        return None
    candidate = labels[0]
    if candidate in IGNORED_SUBDOMAINS:
        return None
    return candidate


def _resolve_tenant_slug(request: Request, root_domain: str) -> str | None:
    host = request.url.hostname or ""
    subdomain = _extract_subdomain(host, root_domain)
    if subdomain:
        return subdomain

    header_slug = request.headers.get("X-Tenant-Slug")
    if header_slug and header_slug.strip():
        return header_slug.strip().lower()

    query_slug = request.query_params.get("slug")
    if query_slug and query_slug.strip():
        return query_slug.strip().lower()

    return None


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    """Resolves the tenant workspace for a request and binds it to TenantContextService.

    Runs AFTER RBACMiddleware (see main.py) so request.state.tenant_id — set from
    the caller's JWT — is already available. Resolution order: explicit
    subdomain/header/query slug, falling back to the JWT's tenant_id when no
    explicit slug is given (the common case: authenticated API calls don't need
    to also send X-Tenant-Slug). An explicit slug that disagrees with the JWT's
    tenant is rejected rather than letting either one silently win.

    No-ops entirely when settings.multi_tenant_enabled is False — row-level tenant
    scoping (request_tenant_id / repositories) is unconditional and unaffected by
    this flag; this middleware only gates subdomain/header resolution and the
    plan/feature-flag context that TenantContextService backs.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        settings = get_settings()

        if (
            not settings.multi_tenant_enabled
            or path in PUBLIC_PATHS
            or path in AUTH_PATHS
            or path in SELF_AUTHENTICATING_PATHS
            or path.startswith(ADMIN_PATH_PREFIX)
            or request.method == "OPTIONS"
        ):
            return await call_next(request)

        started = time.perf_counter()
        jwt_tenant_id = getattr(request.state, "tenant_id", None)
        explicit_slug = _resolve_tenant_slug(request, settings.tenant_root_domain)

        if explicit_slug and jwt_tenant_id and explicit_slug != jwt_tenant_id:
            logger.warning(
                "tenant_mismatch", path=path, jwt_tenant=jwt_tenant_id, slug=explicit_slug,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Tenant workspace does not match the authenticated account"},
            )

        slug = explicit_slug or jwt_tenant_id
        if not slug:
            logger.warning("tenant_slug_missing", path=path)
            return JSONResponse(status_code=404, content={"detail": "Tenant not found"})

        # An explicit slug (subdomain/header/query) resolves by the human-facing slug
        # column; the pure-JWT fallback (no explicit slug given — the common case for
        # a same-origin SPA) resolves by id instead, since a tenant's id and slug are
        # only guaranteed equal for tenants created through TenantService, not for the
        # seeded 'default'/'admin' tenant.
        tenant = await get_tenant_by_slug(explicit_slug) if explicit_slug else await get_tenant_by_id(slug)
        if tenant is None:
            logger.warning("tenant_not_found", slug=slug, path=path)
            return JSONResponse(status_code=404, content={"detail": "Tenant not found"})

        if tenant.status == "suspended":
            logger.warning("tenant_suspended", tenant_id=tenant.tenant_id, slug=slug, path=path)
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "This workspace has been suspended. Contact your workspace "
                        "administrator to restore access."
                    )
                },
            )
        if tenant.status == "offboarded":
            logger.warning("tenant_offboarded", tenant_id=tenant.tenant_id, slug=slug, path=path)
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "This workspace has been offboarded and is no longer accessible."
                    )
                },
            )

        ctx = TenantContext(
            tenant_id=tenant.tenant_id,
            slug=tenant.slug,
            plan=tenant.plan,
            features=tenant.features,
        )
        token = TenantContextService.set_context(ctx)
        request.state.tenant_slug = ctx.slug
        try:
            return await call_next(request)
        finally:
            TenantContextService.reset_context(token)
            logger.info(
                "tenant_resolved",
                tenant_id=ctx.tenant_id,
                slug=ctx.slug,
                plan=ctx.plan,
                path=path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
