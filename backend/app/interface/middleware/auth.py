from __future__ import annotations

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from app.config import get_settings

logger = structlog.get_logger(__name__)

# Paths that never require authentication
PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/metrics"})
# Internal paths called by Orthanc (within Docker network)
INTERNAL_PATHS = frozenset({"/api/orthanc/notify-stable-study"})
# Auth paths that must be public
AUTH_PATHS = frozenset({"/api/auth/login", "/api/auth/register", "/api/auth/mfa/verify"})
# Routes that authenticate themselves (a per-tenant API key, not a platform JWT
# or the global admin api_key) — they must bypass this middleware's own auth
# entirely rather than have it reject/misinterpret their bearer token.
SELF_AUTHENTICATING_PATHS = frozenset({"/api/dicom/upload"})


class RBACMiddleware(BaseHTTPMiddleware):
    """Authentication middleware supporting JWT, API-key, and no-auth modes.

    Configured via AUTH_MODE setting:
    - "jwt": Requires valid JWT Bearer token (from /api/auth/login)
    - "api_key": Requires API key in Authorization or X-API-Key header
    - "none": No authentication (development mode)
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Always allow public paths, auth paths, internal paths, and CORS preflight.
        # /api/dicomweb/* is the OHIF series-list QIDO filter — it must be reachable
        # by the viewer without our JWT, and exposes only data already served
        # unauthenticated via the nginx /dicom-web route straight to Orthanc.
        if (
            path in PUBLIC_PATHS
            or path in INTERNAL_PATHS
            or path in AUTH_PATHS
            or path in SELF_AUTHENTICATING_PATHS
            or path.startswith("/api/dicomweb/")
            or request.method == "OPTIONS"
        ):
            request.state.user = "anonymous"
            request.state.user_id = ""
            request.state.roles = ["viewer"]
            request.state.tenant_id = "default"
            request.state.is_platform_admin = False
            request.state.is_platform_operator = False
            request.state.impersonated_by = None
            if path in INTERNAL_PATHS:
                request.state.user = "orthanc_internal"
                request.state.roles = ["system"]
            return await call_next(request)

        settings = get_settings()
        auth_mode = settings.auth_mode

        if auth_mode == "none":
            # Development mode — full access
            request.state.user = "system"
            request.state.user_id = ""
            request.state.roles = ["admin"]
            request.state.tenant_id = "default"
            request.state.is_platform_admin = True
            request.state.is_platform_operator = True
            request.state.impersonated_by = None
            return await call_next(request)

        if auth_mode == "jwt":
            return await self._handle_jwt_auth(request, call_next)

        # Default: api_key mode (backward compatible)
        return await self._handle_api_key_auth(request, call_next)

    async def _handle_jwt_auth(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        token = self._extract_bearer_token(request)
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

        if payload.get("purpose") == "mfa_pending":
            # A validly-signed token, but scoped to nothing except completing
            # login at POST /auth/mfa/verify (which reads it from the request
            # BODY, never this header) — it carries no role/username claims,
            # so without this check it would silently authenticate as a
            # default-fallback "viewer" for up to its 5-minute lifetime,
            # letting a leaked pre-MFA token make real API calls.
            return JSONResponse(
                status_code=401,
                content={"detail": "MFA verification required before this token can be used"},
            )

        impersonated_by = payload.get("impersonated_by")
        if impersonated_by:
            # Only impersonation tokens pay the Redis round-trip — the hot
            # path for ordinary tokens stays a pure in-memory JWT verify.
            from app.infrastructure.ratelimit.impersonation_blocklist import is_blocked
            jti = payload.get("jti", "")
            if jti and await is_blocked(jti):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "This impersonation session has been ended"},
                )

        request.state.user = payload.get("username", "unknown")
        request.state.user_id = payload.get("sub", "")
        request.state.roles = [payload.get("role", "viewer")]
        request.state.tenant_id = payload.get("tenant_id", "default")
        request.state.is_platform_admin = bool(payload.get("is_platform_admin", False))
        request.state.is_platform_operator = bool(payload.get("is_platform_operator", False))
        request.state.impersonated_by = impersonated_by
        request.state.jti = payload.get("jti")
        request.state.token_exp = payload.get("exp")

        # Check role-based access for admin paths
        if request.url.path.startswith("/api/admin"):
            if payload.get("role") not in ("admin",):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Admin access required"},
                )

        return await call_next(request)

    async def _handle_api_key_auth(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()
        api_key = settings.api_key

        if api_key:
            provided_key = self._extract_api_key(request)
            if not provided_key or provided_key != api_key:
                logger.warning("auth_rejected", path=request.url.path, reason="invalid_or_missing_api_key")
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
            request.state.user = "api_user"
            request.state.user_id = ""
            request.state.roles = ["admin"]
            request.state.tenant_id = "default"
            request.state.is_platform_admin = True
            request.state.is_platform_operator = True
            request.state.impersonated_by = None
        else:
            # No API key configured — development mode, grant full access
            request.state.user = "system"
            request.state.user_id = ""
            request.state.roles = ["admin"]
            request.state.tenant_id = "default"
            request.state.is_platform_admin = True
            request.state.is_platform_operator = True
            request.state.impersonated_by = None

        return await call_next(request)

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
    """Cross-tenant admin surfaces (tenant lifecycle, tenant API keys, plan
    features) require this — the ordinary "role=='admin'" check RBACMiddleware
    already applies to /api/admin/* is per-tenant, so any tenant's own admin
    user would otherwise satisfy it for every OTHER tenant too. This is a
    distinct claim, granted only by an explicit platform-admin flag (see
    AuthService.set_platform_admin), not any tenant role.
    """
    from fastapi import HTTPException

    if not getattr(request.state, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin access required")
    return getattr(request.state, "user", "unknown")


async def require_platform_operator(request: Request) -> str:
    """Gates impersonation — a platform admin is implicitly also an operator
    (full access includes the more limited operator capability), but a plain
    operator cannot manage tenant lifecycle, promote/demote admins, or wipe
    all-tenant data.
    """
    from fastapi import HTTPException

    if not (
        getattr(request.state, "is_platform_admin", False)
        or getattr(request.state, "is_platform_operator", False)
    ):
        raise HTTPException(status_code=403, detail="Platform operator access required")
    return getattr(request.state, "user", "unknown")


def require_permission(permission: str):
    """Dependency enforcing a single RBAC permission key (see domain.permissions).

    Resolves the caller's role(s) (set on request.state by RBACMiddleware) to the
    per-tenant permission set in the `roles` table. `admin`/`system` always pass —
    so dev modes (auth_mode none/api_key, which set role=admin) and the seeded
    admin user are never blocked, keeping existing flows working.
    """
    from fastapi import Depends, HTTPException

    async def check_permission(request: Request):
        user_roles = getattr(request.state, "roles", []) or []
        # Admin / system bypass (also covers auth_mode none/api_key).
        if "admin" in user_roles or "system" in user_roles:
            return getattr(request.state, "user", "system")

        tenant_id = getattr(request.state, "tenant_id", "default") or "default"

        from sqlalchemy import select

        from app.infrastructure.database.models import RoleRecord
        from app.infrastructure.database.session import async_session_factory

        granted: set[str] = set()
        async with async_session_factory() as session:
            res = await session.execute(
                select(RoleRecord).where(
                    RoleRecord.tenant_id == tenant_id,
                    RoleRecord.name.in_(user_roles),
                )
            )
            for role in res.scalars():
                granted.update(role.permissions or [])

        if permission not in granted:
            raise HTTPException(status_code=403, detail=f"Permission required: {permission}")
        return getattr(request.state, "user", "unknown")

    return Depends(check_permission)
