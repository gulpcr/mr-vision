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
AUTH_PATHS = frozenset({"/api/auth/login", "/api/auth/register"})


class RBACMiddleware(BaseHTTPMiddleware):
    """Authentication middleware supporting JWT, API-key, and no-auth modes.

    Configured via AUTH_MODE setting:
    - "jwt": Requires valid JWT Bearer token (from /api/auth/login)
    - "api_key": Requires API key in Authorization or X-API-Key header
    - "none": No authentication (development mode)
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Always allow public paths, auth paths, internal paths, and CORS preflight
        if (
            path in PUBLIC_PATHS
            or path in INTERNAL_PATHS
            or path in AUTH_PATHS
            or request.method == "OPTIONS"
        ):
            request.state.user = "anonymous"
            request.state.user_id = ""
            request.state.roles = ["viewer"]
            request.state.tenant_id = "default"
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

        request.state.user = payload.get("username", "unknown")
        request.state.user_id = payload.get("sub", "")
        request.state.roles = [payload.get("role", "viewer")]
        request.state.tenant_id = payload.get("tenant_id", "default")

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
        else:
            # No API key configured — development mode, grant full access
            request.state.user = "system"
            request.state.user_id = ""
            request.state.roles = ["admin"]
            request.state.tenant_id = "default"

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
