from __future__ import annotations

import hmac

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from app.config import get_settings
from app.domain.permissions import WILDCARD, has_permission, is_referral_scoped
from app.infrastructure.tenant.db_scope import (
    bind_platform_scope,
    bind_tenant_scope,
    clear_scope,
    reset_scope,
)

logger = structlog.get_logger(__name__)

# Paths that never require authentication
PUBLIC_PATHS = frozenset(
    {"/health", "/docs", "/openapi.json", "/redoc", "/metrics", "/api/cortex"}
)
# Internal paths called by Orthanc (within Docker network)
INTERNAL_PATHS = frozenset({"/api/orthanc/notify-stable-study"})
# Auth paths that must be public. They run before any tenant is known (login resolves
# the workspace itself), so they get a platform DB scope and filter explicitly.
AUTH_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/mfa/verify",
    "/api/auth/invitations/accept",
    "/api/auth/refresh",
    "/api/tenant/public-branding",
})
# Viewer endpoints authenticate with the httpOnly viewer-session cookie (OHIF and the
# nginx auth_request hook cannot send our bearer token) and bind their own DB scope.
VIEWER_COOKIE_PATHS = frozenset({"/api/internal/dicomweb-authz"})
VIEWER_COOKIE_PREFIXES = ("/api/dicomweb/",)
# Routes that authenticate themselves (a per-tenant API key, not a platform JWT or the
# global admin api_key) — they must bypass this middleware's own auth entirely rather
# than have it reject/misinterpret their bearer token.
SELF_AUTHENTICATING_PATHS = frozenset({"/api/dicom/upload"})
# What an account restricted to "fix your sign-in first" (forced password change / forced
# MFA enrolment) may still call: its own identity, the fix itself, and signing out.
RESTRICTED_SESSION_PATHS = frozenset({
    "/api/auth/me",
    "/api/auth/me/permissions",
    "/api/auth/change-password",
    "/api/auth/logout",
    "/api/auth/viewer-session",
    "/api/auth/mfa/enroll",
    "/api/auth/mfa/confirm",
    "/api/tenant/current",
})


def _set_anonymous(request: Request) -> None:
    request.state.user = "anonymous"
    request.state.user_id = ""
    request.state.roles = []
    request.state.permissions = frozenset()
    request.state.tenant_id = "default"
    request.state.is_platform_admin = False
    request.state.is_platform_operator = False
    request.state.impersonated_by = None
    request.state.referral_scoped = False
    request.state.allowed_usecases = None


def _set_superuser(request: Request, user: str) -> None:
    """Development auth modes (none / api_key): full access, Admin tenant."""
    request.state.user = user
    request.state.user_id = ""
    request.state.roles = ["admin"]
    request.state.permissions = frozenset({WILDCARD})
    request.state.tenant_id = "default"
    request.state.is_platform_admin = True
    request.state.is_platform_operator = True
    request.state.impersonated_by = None
    request.state.referral_scoped = False
    request.state.allowed_usecases = None


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# The UI sends ``X-Requested-With: mrcv`` on every API call (ui/src/lib/session.ts).
CSRF_HEADER_VALUE = "mrcv"

class RBACMiddleware(BaseHTTPMiddleware):
    """Authentication middleware supporting JWT, API-key, and no-auth modes.

    Configured via AUTH_MODE setting:
    - "jwt": Requires valid JWT Bearer token (from /api/auth/login)
    - "api_key": Requires API key in Authorization or X-API-Key header
    - "none": No authentication (development mode)

    In jwt mode every request is resolved to a live Principal from the database
    (infrastructure/auth/principal.py): the account must still be active, the token's
    ``tv`` must match the user's ``token_version`` (revocation), and the role's current
    permissions — not the token's cached role claim — are what require_permission checks.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Always allow public paths, auth paths, internal paths, and CORS preflight.
        # Viewer paths (/api/dicomweb/*, the DICOMweb authz hook) skip bearer auth here
        # and authenticate the viewer-session cookie themselves (viewer_access.py).
        if (
            path in PUBLIC_PATHS
            or path in INTERNAL_PATHS
            or path in AUTH_PATHS
            or path in SELF_AUTHENTICATING_PATHS
            or path in VIEWER_COOKIE_PATHS
            or path.startswith(VIEWER_COOKIE_PREFIXES)
            or request.method == "OPTIONS"
        ):
            _set_anonymous(request)
            if path in INTERNAL_PATHS:
                request.state.user = "orthanc_internal"
                request.state.roles = ["system"]
                request.state.permissions = frozenset({WILDCARD})
            # DB scope (Row-Level Security): login/MFA/register must look a user up
            # before any tenant is known, the Orthanc webhook resolves the study's
            # tenant itself, and the DICOM upload route narrows to its API key's
            # tenant after validating it — all start cross-tenant and are responsible
            # for filtering explicitly. Everything else on this branch (health, docs,
            # viewer-cookie routes) gets no tenant at all.
            if path in AUTH_PATHS or path in INTERNAL_PATHS or path in SELF_AUTHENTICATING_PATHS:
                token = bind_platform_scope()
            else:
                token = clear_scope()
            try:
                return await call_next(request)
            finally:
                reset_scope(token)

        settings = get_settings()
        auth_mode = settings.auth_mode

        if auth_mode == "none":
            _set_superuser(request, "system")
            return await self._call_in_tenant_scope(request, call_next)

        if auth_mode == "jwt":
            return await self._handle_jwt_auth(request, call_next)

        # Default: api_key mode (backward compatible)
        return await self._handle_api_key_auth(request, call_next)

    async def _handle_jwt_auth(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        token = self._extract_bearer_token(request)
        if not token:
            token = request.cookies.get(get_settings().access_cookie_name) or None
            # A cookie is sent by the browser on its own, so a state-changing request
            # authenticated by it must also prove it came from our own scripts (a
            # cross-site form or fetch cannot set this header without a CORS preflight).
            if token and request.method not in _SAFE_METHODS and (
                request.headers.get("x-requested-with", "").lower() != CSRF_HEADER_VALUE
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Missing X-Requested-With header", "code": "csrf"},
                )
        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing authentication token"},
            )

        from app.application.auth_service import AuthService
        auth_service = AuthService(session=None)
        payload = auth_service.decode_token(token)
        if not payload:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        if payload.get("purpose") == "viewer":
            # Viewer-session cookie token (DICOMweb/WebSocket only) — never an API
            # credential, even if someone copies it into an Authorization header.
            return JSONResponse(
                status_code=401,
                content={"detail": "Viewer session tokens cannot be used for API access"},
            )

        if payload.get("purpose") == "mfa_pending":
            # A validly-signed token, but scoped to nothing except completing login at
            # POST /auth/mfa/verify (which reads it from the request BODY, never this
            # header) — it carries no role/username claims, so without this check it
            # would silently authenticate as a default-fallback "viewer" for up to its
            # 5-minute lifetime, letting a leaked pre-MFA token make real API calls.
            return JSONResponse(
                status_code=401,
                content={"detail": "MFA verification required before this token can be used"},
            )

        impersonated_by = payload.get("impersonated_by")
        # Ended sessions: signed-out tokens and stopped impersonations are blocklisted
        # by jti until they expire (Redis; fails open like the other limiters).
        from app.infrastructure.ratelimit.impersonation_blocklist import is_blocked
        jti = payload.get("jti", "")
        if jti and await is_blocked(jti):
            return JSONResponse(
                status_code=401,
                content={"detail": "This session has ended — please sign in again"},
            )

        from app.infrastructure.auth.principal import load_principal

        tenant_id = payload.get("tenant_id") or "default"
        principal = await load_principal(payload.get("sub", ""), tenant_id)
        if principal is None or int(payload.get("tv", 0)) != principal.token_version:
            return JSONResponse(
                status_code=401,
                content={"detail": "This session is no longer valid — please sign in again"},
            )

        request.state.user = principal.username
        request.state.user_id = principal.user_id
        request.state.roles = [principal.role]
        request.state.permissions = principal.permissions
        request.state.tenant_id = tenant_id
        # Impersonation borrows the target's own tenant permissions, never platform power.
        request.state.is_platform_admin = principal.is_platform_admin and not impersonated_by
        request.state.is_platform_operator = principal.is_platform_operator and not impersonated_by
        request.state.impersonated_by = impersonated_by
        request.state.referral_scoped = is_referral_scoped(principal.permissions)
        request.state.allowed_usecases = principal.allowed_usecases
        request.state.jti = payload.get("jti")
        request.state.token_exp = payload.get("exp")
        request.state.token_version = principal.token_version
        request.state.must_change_password = principal.must_change_password
        request.state.mfa_enrollment_required = principal.mfa_enrollment_required

        # An operator impersonating the account is not subject to its sign-in fixes.
        if not impersonated_by and request.url.path not in RESTRICTED_SESSION_PATHS:
            if principal.must_change_password:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "You must change your password before continuing",
                             "code": "password_change_required"},
                )
            if principal.mfa_enrollment_required:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Set up two-factor authentication before continuing",
                             "code": "mfa_enrollment_required"},
                )

        return await self._call_in_tenant_scope(request, call_next)

    async def _handle_api_key_auth(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()
        api_key = settings.api_key

        if api_key:
            provided_key = self._extract_api_key(request)
            if not provided_key or not hmac.compare_digest(provided_key, api_key):
                logger.warning("auth_rejected", path=request.url.path, reason="invalid_or_missing_api_key")
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
            _set_superuser(request, "api_user")
        else:
            # No API key configured — development mode, grant full access
            _set_superuser(request, "system")

        return await self._call_in_tenant_scope(request, call_next)

    @staticmethod
    async def _call_in_tenant_scope(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Run the rest of the request with the caller's tenant bound as the DB scope,
        so Row-Level Security confines every query to that tenant — and, for a
        referral-scoped user (the referring Doctor), to studies they referred. Platform
        routes widen it explicitly via require_platform_admin/_operator."""
        referring = (
            getattr(request.state, "user_id", None)
            if getattr(request.state, "referral_scoped", False)
            else None
        )
        token = bind_tenant_scope(
            getattr(request.state, "tenant_id", None) or "default", referring_user_id=referring
        )
        try:
            return await call_next(request)
        finally:
            reset_scope(token)

    @staticmethod
    def _extract_bearer_token(request: Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    @staticmethod
    def _extract_api_key(request: Request) -> str | None:
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return api_key
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None


def require_role(*allowed_roles: str):
    """Dependency that checks if the current user has one of the allowed roles."""
    from fastapi import Depends, HTTPException

    async def check_role(request: Request):
        user_roles = getattr(request.state, "roles", [])
        if not any(r in allowed_roles for r in user_roles):
            raise HTTPException(
                status_code=403,
                detail=f"Required role: {', '.join(allowed_roles)}",
            )
        return request.state.user

    return Depends(check_role)


async def require_platform_admin(request: Request) -> str:
    """Cross-tenant admin surfaces (tenant lifecycle, tenant API keys, plan features)
    require this — a tenant's own admin role only has authority inside that tenant.
    This is a distinct flag, honoured only for accounts of the platform (Admin)
    tenant (see infrastructure/auth/principal.py), never for impersonation tokens.
    """
    from fastapi import HTTPException

    if not getattr(request.state, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin access required")
    # Cross-tenant surface: widen the DB scope so RLS exposes every tenant's rows for
    # the rest of this request. Bound for the request's lifetime (FastAPI resolves this
    # dependency in the same context as the endpoint); the caller's own tenant is kept
    # as the default for row stamping.
    bind_platform_scope(getattr(request.state, "tenant_id", None))
    return getattr(request.state, "user", "unknown")


async def require_platform_operator(request: Request) -> str:
    """Gates impersonation — a platform admin is implicitly also an operator (full
    access includes the more limited operator capability), but a plain operator
    cannot manage tenant lifecycle, promote/demote admins, or wipe all-tenant data.
    """
    from fastapi import HTTPException

    if not (
        getattr(request.state, "is_platform_admin", False)
        or getattr(request.state, "is_platform_operator", False)
    ):
        raise HTTPException(status_code=403, detail="Platform operator access required")
    bind_platform_scope(getattr(request.state, "tenant_id", None))
    return getattr(request.state, "user", "unknown")


def require_permission(permission: str):
    """Dependency enforcing an RBAC permission (see domain.permissions for the grammar,
    including ``*`` / ``resource.*`` grants and ``a||b`` alternatives).

    Checks the live permissions RBACMiddleware resolved for this request from the
    caller's tenant role — no per-check database query.
    """
    from fastapi import Depends, HTTPException

    async def check_permission(request: Request):
        granted = getattr(request.state, "permissions", None) or frozenset()
        if not has_permission(granted, permission):
            raise HTTPException(status_code=403, detail=f"Permission required: {permission}")
        return getattr(request.state, "user", "unknown")

    check_permission.required_permission = permission  # introspected by the route-coverage test
    return Depends(check_permission)
