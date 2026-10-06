from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import client_ip_from
from app.domain.audit_state import state_change_details
from app.application.auth_service import (
    AccountLockedError,
    PasswordPolicyError,
    AmbiguousAccountError,
    AuthService,
    InvitationError,
    SeatLimitError,
    TenantAccessError,
)
from app.application.impersonation_service import ImpersonationService
from app.application.mfa_service import MfaLockedError, MfaService
from app.domain.enums import AuditAction
from app.domain.models import AuditEntry
from app.infrastructure.database.repositories import PgAuditRepository
from app.interface.api.dependencies import get_session
from app.infrastructure.tenant.db_scope import bind_platform_scope
from app.interface.middleware.auth import (
    require_permission,
    require_platform_admin,
    require_platform_operator,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)
    # Workspace (tenant slug). Optional: also taken from the subdomain or the
    # X-Tenant-Slug header; required only when the username exists in several
    # workspaces.
    workspace: str | None = Field(default=None, max_length=128)


class InviteRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=128)
    email: str = Field(..., max_length=256)
    role: str = Field(..., min_length=1, max_length=64)
    full_name: str = ""


class AcceptInvitationRequest(BaseModel):
    token: str = Field(..., min_length=16, max_length=256)
    password: str = Field(..., min_length=10, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=128)
    email: str = Field(..., max_length=256)
    password: str = Field(..., min_length=8)
    full_name: str = ""


class LoginResponse(BaseModel):
    """Either a real token (mfa_required=False) or, when the account has MFA
    enabled, a short-lived mfa_token to be exchanged at /mfa/verify — never both,
    and never a real access_token without a code being verified first."""

    mfa_required: bool = False
    mfa_token: str | None = None
    access_token: str | None = None
    token_type: str = "bearer"
    user_id: str | None = None
    username: str | None = None
    role: str | None = None
    tenant_id: str | None = None
    must_change_password: bool = False
    mfa_enrollment_required: bool = False
    # Unix time the access token (and its httpOnly cookie) expires; the UI schedules its
    # refresh from this instead of reading the token.
    expires_at: int | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def _policy_exception(e: PasswordPolicyError) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"message": "Password does not meet the policy", "problems": e.problems},
    )


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str = Field(..., min_length=6, max_length=16)


class MfaEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_code_data_uri: str


class MfaConfirmRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=16)


class MfaConfirmResponse(BaseModel):
    recovery_codes: list[str]


class MfaDisableRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=16)


class PlatformAdminUpdateRequest(BaseModel):
    is_platform_admin: bool


class PlatformOperatorUpdateRequest(BaseModel):
    is_platform_operator: bool


class ImpersonationTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    role: str
    tenant_id: str
    expires_in_minutes: int


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    full_name: str
    role: str
    tenant_id: str
    is_active: bool
    is_platform_admin: bool = False
    is_platform_operator: bool = False
    totp_enabled: bool = False
    created_at: str | None = None
    status: str = "active"
    must_change_password: bool = False
    last_login_at: str | None = None


def _to_user_response(user) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        tenant_id=user.tenant_id,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        is_platform_operator=user.is_platform_operator,
        totp_enabled=user.totp_enabled,
        created_at=user.created_at.isoformat() if user.created_at else None,
        status=getattr(user, "status", "active") or "active",
        must_change_password=bool(getattr(user, "must_change_password", False)),
        last_login_at=(
            user.last_login_at.isoformat() if getattr(user, "last_login_at", None) else None
        ),
    )


def _set_viewer_cookie(
    response: Response, user_id: str, tenant_id: str, is_platform_admin: bool,
    referral_scoped: bool = False, token_version: int = 0,
) -> None:
    """Issue the httpOnly viewer-session cookie (see interface/api/viewer_access.py)."""
    from app.config import get_settings

    settings = get_settings()
    token = AuthService(session=None).create_viewer_token(
        subject=user_id, tenant_id=tenant_id, is_platform_admin=is_platform_admin,
        referral_scoped=referral_scoped, token_version=token_version,
    )
    response.set_cookie(
        key=settings.viewer_cookie_name,
        value=token,
        max_age=settings.jwt_access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.viewer_cookie_secure,
        samesite="strict",
        path="/",
    )


async def _set_viewer_cookie_from_login(response: Response, result: dict) -> None:
    # No image access until a forced password change / MFA enrolment is resolved.
    restricted = result.get("must_change_password") or result.get("mfa_enrollment_required")
    if result.get("access_token") and not restricted:
        from app.domain.permissions import is_referral_scoped
        from app.infrastructure.auth.principal import load_principal

        payload = AuthService(session=None).decode_token(result["access_token"]) or {}
        principal = await load_principal(result["user_id"], result["tenant_id"])
        _set_viewer_cookie(
            response, result["user_id"], result["tenant_id"],
            bool(payload.get("is_platform_admin", False)),
            referral_scoped=bool(principal and is_referral_scoped(principal.permissions)),
            token_version=principal.token_version if principal else 0,
        )


def _session_response(response: Response, result: dict) -> LoginResponse:
    """Set the httpOnly access-token cookie for a freshly issued token and build the
    JSON answer (without the token itself when ACCESS_TOKEN_IN_BODY is off)."""
    from app.config import get_settings

    settings = get_settings()
    token = result.get("access_token")
    body = dict(result)
    if token:
        payload = AuthService(session=None).decode_token(token) or {}
        exp = int(payload.get("exp", 0) or 0) or None
        body["expires_at"] = exp
        response.set_cookie(
            key=settings.access_cookie_name,
            value=token,
            max_age=settings.jwt_access_token_expire_minutes * 60,
            httponly=True,
            secure=settings.viewer_cookie_secure,
            samesite="strict",
            path="/",
        )
        if not settings.access_token_in_body:
            body["access_token"] = None
    return LoginResponse(**body)


def _clear_access_cookie(response: Response) -> None:
    from app.config import get_settings

    response.delete_cookie(get_settings().access_cookie_name, path="/")


def _set_refresh_cookie(response: Response, token: str) -> None:
    from app.config import get_settings

    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=settings.viewer_cookie_secure,
        samesite="strict",
        path="/api/auth",  # only ever sent to /api/auth/refresh and /api/auth/logout
    )


def _clear_refresh_cookie(response: Response) -> None:
    from app.config import get_settings

    response.delete_cookie(get_settings().refresh_cookie_name, path="/api/auth")


async def _start_session(request: Request, response: Response, session, result: dict) -> None:
    """After a successful sign-in: begin a refresh-session family and set its cookie."""
    from app.application.session_service import RefreshSessionService

    if not result.get("access_token"):
        return
    raw = await RefreshSessionService(session).start(
        result["user_id"], result["tenant_id"], int(result.get("token_version", 0) or 0),
        client_ip=client_ip_from(request), user_agent=request.headers.get("user-agent", ""),
    )
    _set_refresh_cookie(response, raw)


async def _resolve_login_tenant(request: Request, workspace: str | None) -> str | None:
    """Tenant id for a login: explicit workspace, else subdomain / X-Tenant-Slug.
    None = not specified (login then succeeds only if the username is unambiguous)."""
    from app.config import get_settings
    from app.infrastructure.tenant.repository import get_tenant_by_slug
    from app.interface.middleware.tenant import _resolve_tenant_slug

    slug = (workspace or "").strip().lower() or _resolve_tenant_slug(
        request, get_settings().tenant_root_domain
    )
    if not slug:
        return None
    tenant = await get_tenant_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown workspace")
    return tenant.tenant_id


def _mfa_locked_exception(e: MfaLockedError) -> HTTPException:
    return HTTPException(
        status_code=423,
        detail=f"Too many failed attempts. Try again in {e.retry_after_seconds} seconds.",
        headers={"Retry-After": str(e.retry_after_seconds)},
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Authenticate user and return a JWT token, or an mfa_token if MFA is enabled."""
    from app.infrastructure.ratelimit.login_lockout import ip_allowed

    client_ip = client_ip_from(request)
    if not await ip_allowed(client_ip):
        raise HTTPException(
            status_code=429, detail="Too many sign-in attempts — wait a minute and try again",
            headers={"Retry-After": "60"},
        )
    tenant_id = await _resolve_login_tenant(request, body.workspace)
    service = AuthService(session)
    try:
        result = await service.authenticate(
            body.username, body.password, tenant_id=tenant_id, client_ip=client_ip
        )
    except TenantAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except AmbiguousAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountLockedError as e:
        await session.commit()  # keep the audit entry: raising rolls the session back
        raise HTTPException(
            status_code=423, detail=str(e), headers={"Retry-After": str(e.retry_after)}
        )
    if not result:
        await session.commit()  # keep the failed-login audit entry
        raise HTTPException(status_code=401, detail="Invalid credentials")
    await _start_session(request, response, session, result)
    await _set_viewer_cookie_from_login(response, result)
    return _session_response(response, result)


@router.post("/mfa/verify", response_model=LoginResponse)
async def verify_mfa(
    body: MfaVerifyRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Second step of login when the account has MFA enabled."""
    service = AuthService(session)
    try:
        result = await service.complete_mfa_login(body.mfa_token, body.code)
    except MfaLockedError as e:
        raise _mfa_locked_exception(e)
    if not result:
        await session.commit()  # keep the mfa_failed audit entry
        raise HTTPException(status_code=401, detail="Invalid or expired MFA session, or wrong code")
    await _start_session(request, response, session, result)
    await _set_viewer_cookie_from_login(response, result)
    return _session_response(response, result)


@router.get("/me/permissions")
async def my_permissions(request: Request):
    """The caller's live effective permissions (from their role's current definition)
    — the UI gates navigation, routes and actions on this instead of a hard-coded
    role map, so custom roles and edited system roles take effect immediately."""
    from app.domain.permissions import has_permission

    permissions = sorted(getattr(request.state, "permissions", None) or [])
    return {
        "user_id": getattr(request.state, "user_id", ""),
        "role": (getattr(request.state, "roles", None) or [None])[0],
        "tenant_id": getattr(request.state, "tenant_id", None),
        "permissions": permissions,
        "referral_scoped": bool(getattr(request.state, "referral_scoped", False)),
        "is_platform_admin": bool(getattr(request.state, "is_platform_admin", False)),
        "is_platform_operator": bool(getattr(request.state, "is_platform_operator", False)),
        "is_admin": has_permission(permissions, "*"),
        "must_change_password": bool(getattr(request.state, "must_change_password", False)),
        "mfa_enrollment_required": bool(
            getattr(request.state, "mfa_enrollment_required", False)
        ),
    }


@router.post("/change-password", response_model=LoginResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Self-service password change (also resolves a forced change). Ends every other
    session and returns a fresh token for this one."""
    from app.infrastructure.auth.principal import invalidate_principal

    user_id = getattr(request.state, "user_id", "") or ""
    if not user_id or getattr(request.state, "impersonated_by", None):
        raise HTTPException(status_code=403, detail="Password can only be changed by its owner")
    service = AuthService(session)
    try:
        result = await service.change_password(
            user_id, request.state.tenant_id, body.current_password, body.new_password
        )
    except PasswordPolicyError as e:
        raise _policy_exception(e)
    if result is None:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    # Commit before dropping the cached principal: the session would otherwise commit
    # only after the response, and a request in between would re-cache the old state.
    await _start_session(request, response, session, result)
    await session.commit()
    invalidate_principal(user_id)
    await _set_viewer_cookie_from_login(response, result)
    return _session_response(response, result)


@router.post("/refresh", response_model=LoginResponse)
async def refresh_session(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Continue a session: exchange the refresh cookie for a new access token (and a
    rotated refresh cookie). Fails once the session is idle, expired, revoked, or the
    token is replayed — the UI then signs out."""
    from app.application.session_service import RefreshSessionService, SessionRejected
    from app.config import get_settings

    raw = request.cookies.get(get_settings().refresh_cookie_name, "")
    if not raw:
        raise HTTPException(status_code=401, detail="No active session")
    try:
        refreshed = await RefreshSessionService(session).rotate(
            raw, client_ip=client_ip_from(request), user_agent=request.headers.get("user-agent", ""),
        )
    except SessionRejected as e:
        await session.commit()  # keep revocations / reuse audit
        rejection = JSONResponse(
            status_code=401, content={"detail": "Your session has ended — please sign in again",
                                      "reason": e.reason},
        )
        _clear_refresh_cookie(rejection)
        _clear_access_cookie(rejection)
        return rejection
    service = AuthService(session)
    user = await service.get_user_by_id(refreshed.user_id)
    if user is None or user.tenant_id != refreshed.tenant_id:
        raise HTTPException(status_code=401, detail="Your session has ended — please sign in again")
    result = await service._issue_tokens(user, audit_login=False)
    _set_refresh_cookie(response, refreshed.refresh_token)
    await _set_viewer_cookie_from_login(response, result)
    return _session_response(response, result)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """End this session server-side: the access token is blocklisted until it would
    have expired, the viewer cookie is dropped, and the sign-out is audited."""
    import time

    from app.config import get_settings
    from app.infrastructure.ratelimit.impersonation_blocklist import block_jti

    jti = getattr(request.state, "jti", None)
    exp = getattr(request.state, "token_exp", None)
    if jti and exp:
        try:
            await block_jti(jti, int(exp) - int(time.time()))
        except Exception:
            raise HTTPException(status_code=503, detail="Could not end the session — try again")
    user_id = getattr(request.state, "user_id", "") or ""
    if user_id:
        await AuthService(session).audit_logout(
            user_id, getattr(request.state, "user", "") or "",
            getattr(request.state, "tenant_id", "default") or "default",
            client_ip_from(request),
        )
    raw_refresh = request.cookies.get(get_settings().refresh_cookie_name, "")
    if raw_refresh:
        from app.application.session_service import RefreshSessionService

        await RefreshSessionService(session).end(raw_refresh, "logout")
    response.delete_cookie(get_settings().viewer_cookie_name, path="/")
    _clear_refresh_cookie(response)
    _clear_access_cookie(response)
    # Tell the browser to drop cached viewer images and site storage (HIPAA 164.312(a)(2)(iii)).
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    response.status_code = 204
    return response


def _ttl_hours() -> int:
    from app.config import get_settings

    return max(1, get_settings().invitation_ttl_hours)


async def _invite_link(request: Request, token: str, tenant_id: str) -> str:
    from app.infrastructure.tenant.repository import get_tenant_by_id

    tenant = await get_tenant_by_id(tenant_id)
    workspace = tenant.slug if tenant else tenant_id
    base = str(request.base_url).rstrip("/")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_host:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        base = f"{proto}://{forwarded_host}"
    return f"{base}/accept-invite?token={token}&workspace={workspace}"


@router.post("/users/invite", status_code=201, dependencies=[require_permission("user.manage")])
async def invite_user(
    body: InviteRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Invite a user into the caller's own workspace with a role defined there. Returns a
    one-time link (valid INVITATION_TTL_HOURS, default 3h) for the admin to send — the platform has no mail relay."""
    tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    if body.role == "admin" and "admin" not in (getattr(request.state, "roles", None) or []):
        raise HTTPException(status_code=403, detail="Only an admin can invite another admin")
    service = AuthService(session)
    try:
        user, token = await service.invite_user(
            tenant_id=tenant_id, username=body.username, email=body.email, role=body.role,
            full_name=body.full_name, invited_by=getattr(request.state, "user", "unknown"),
        )
    except SeatLimitError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {**_to_user_response(user).model_dump(), "status": "invited",
            "invite_link": await _invite_link(request, token, tenant_id),
            "expires_in_hours": _ttl_hours()}


@router.post("/users/{user_id}/reinvite", dependencies=[require_permission("user.manage")])
async def reinvite_user(
    user_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Issue a fresh invitation / password-set link for a user of the caller's workspace."""
    tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    try:
        token = await AuthService(session).issue_password_reset(
            user_id, tenant_id, actor=getattr(request.state, "user", "unknown")
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found")
    return {"invite_link": await _invite_link(request, token, tenant_id),
            "expires_in_hours": _ttl_hours()}


@router.post("/users/{user_id}/revoke-sessions", dependencies=[require_permission("user.manage")])
async def revoke_user_sessions(
    user_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Sign a user of the caller's workspace out everywhere (invalidates all tokens)."""
    from app.infrastructure.auth.principal import invalidate_principal

    tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    if not await AuthService(session).revoke_sessions(
        user_id, tenant_id, actor=getattr(request.state, "user", "unknown")
    ):
        raise HTTPException(status_code=404, detail="User not found")
    await session.commit()  # before invalidating (see change_password)
    invalidate_principal(user_id)
    return {"status": "ok"}


@router.post("/invitations/accept")
async def accept_invitation(
    body: AcceptInvitationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Public: redeem an invitation / password-reset link and set a password."""
    from app.infrastructure.auth.principal import invalidate_principal

    try:
        user = await AuthService(session).accept_invitation(body.token, body.password)
    except PasswordPolicyError as e:
        raise _policy_exception(e)
    except InvitationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TenantAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await session.commit()  # before invalidating (see change_password)
    invalidate_principal(user.id)
    return {"status": "ok", "username": user.username, "tenant_id": user.tenant_id}


@router.post("/viewer-session", status_code=204)
async def refresh_viewer_session(request: Request, response: Response):
    """(Re)issue the viewer-session cookie for the authenticated caller — the UI calls
    this on load so a session restored from storage also has a valid viewer cookie."""
    if getattr(request.state, "must_change_password", False) or getattr(
        request.state, "mfa_enrollment_required", False
    ):
        response.status_code = 204  # restricted session: no image access yet
        return response
    _set_viewer_cookie(
        response,
        getattr(request.state, "user_id", "") or "",
        getattr(request.state, "tenant_id", "default") or "default",
        bool(getattr(request.state, "is_platform_admin", False)),
        referral_scoped=bool(getattr(request.state, "referral_scoped", False)),
        token_version=int(getattr(request.state, "token_version", 0) or 0),
    )
    response.status_code = 204
    return response


@router.delete("/viewer-session", status_code=204)
async def clear_viewer_session(response: Response):
    """Drop the viewer-session cookie (logout)."""
    from app.config import get_settings

    response.delete_cookie(get_settings().viewer_cookie_name, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserResponse)
async def get_me(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """The caller's own live profile — used instead of trusting a stale cached
    login response for MFA/platform-role state, which can change without the
    user re-logging in."""
    service = AuthService(session)
    user = await service.get_user_by_id(getattr(request.state, "user_id", ""))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _to_user_response(user)


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(
    body: RegisterRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Self-registration (default role: viewer, Admin workspace) — disabled unless
    PUBLIC_REGISTRATION_ENABLED. Workspace users are created by invitation."""
    from app.config import get_settings

    if not get_settings().public_registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="Self-registration is disabled — ask your workspace administrator for an invitation",
        )
    service = AuthService(session)
    try:
        service.check_password_policy(body.password, body.username)
    except PasswordPolicyError as e:
        raise _policy_exception(e)
    try:
        user = await service.create_user(
            username=body.username,
            email=body.email,
            password=body.password,
            full_name=body.full_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return _to_user_response(user)


@router.get("/users", response_model=list[UserResponse], dependencies=[require_permission("user.manage")])
async def list_users(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_id: str | None = None,
):
    """List users in the caller's own tenant (requires user.manage). Viewing
    another tenant's users via ?tenant_id= requires platform admin access — this
    used to list every tenant's users unconditionally, which was a cross-tenant
    data leak."""
    caller_tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    target_tenant_id = tenant_id or caller_tenant_id
    if target_tenant_id != caller_tenant_id:
        if not getattr(request.state, "is_platform_admin", False):
            raise HTTPException(403, "Viewing another tenant's users requires platform admin access")
        # Row-Level Security confines the session to the caller's tenant; a platform
        # admin reading another tenant's roster must widen it explicitly.
        bind_platform_scope(caller_tenant_id)
    service = AuthService(session)
    users = await service.list_users(tenant_id=target_tenant_id)
    return [_to_user_response(u) for u in users]


@router.put("/users/{user_id}/role", dependencies=[require_permission("user.manage")])
async def update_user_role(
    user_id: str,
    role: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Update a user's role within the caller's tenant (requires user.manage).

    Only an admin may grant the admin role — user.manage alone must not be a path to
    privilege escalation.
    """
    caller_tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    if role == "admin" and "admin" not in (getattr(request.state, "roles", None) or []):
        raise HTTPException(status_code=403, detail="Only an admin can grant the admin role")
    service = AuthService(session)
    previous = await service.get_user_by_id(user_id)
    previous_role = previous.role if previous is not None else None
    try:
        user = await service.update_user_role(user_id, role, tenant_id=caller_tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    from app.infrastructure.auth.principal import invalidate_principal

    await session.commit()  # before invalidating (see change_password)
    invalidate_principal(user_id)
    await PgAuditRepository(session, tenant_id=caller_tenant_id).save(AuditEntry(
        action=AuditAction.USER_ROLE_CHANGED,
        entity_type="user",
        entity_id=user_id,
        actor=getattr(request.state, "user", "unknown"),
        details={"target_username": user.username, "role": role, "previous_role": previous_role,
                 **state_change_details({"role": previous_role}, {"role": role})},
        tenant_id=caller_tenant_id,
    ))
    return {"status": "ok", "user_id": user_id, "role": role}


@router.delete("/users/{user_id}", dependencies=[require_permission("user.manage")])
async def deactivate_user(
    user_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Deactivate a user account within the caller's tenant (requires user.manage)."""
    caller_tenant_id = getattr(request.state, "tenant_id", "default") or "default"
    service = AuthService(session)
    try:
        user = await service.deactivate_user(user_id, tenant_id=caller_tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    from app.infrastructure.auth.principal import invalidate_principal

    await session.commit()  # before invalidating (see change_password)
    invalidate_principal(user_id)
    await PgAuditRepository(session, tenant_id=caller_tenant_id).save(AuditEntry(
        action=AuditAction.USER_DEACTIVATED,
        entity_type="user",
        entity_id=user_id,
        actor=getattr(request.state, "user", "unknown"),
        details={"target_username": user.username},
        tenant_id=caller_tenant_id,
    ))
    return {"status": "ok", "user_id": user_id}


@router.put("/users/{user_id}/platform-admin", response_model=UserResponse)
async def update_platform_admin_status(
    user_id: str,
    body: PlatformAdminUpdateRequest,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Grant/revoke platform-admin status. Only an existing platform admin may
    call this — there is no self-bootstrap path."""
    service = AuthService(session)
    user = await service.set_platform_admin(user_id, body.is_platform_admin)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await PgAuditRepository(session).save(AuditEntry(
        action=AuditAction.PLATFORM_ADMIN_GRANTED if body.is_platform_admin else AuditAction.PLATFORM_ADMIN_REVOKED,
        entity_type="user",
        entity_id=user_id,
        actor=actor,
        details={"target_username": user.username},
        tenant_id=user.tenant_id,
    ))
    return _to_user_response(user)


@router.put("/users/{user_id}/platform-operator", response_model=UserResponse)
async def update_platform_operator_status(
    user_id: str,
    body: PlatformOperatorUpdateRequest,
    actor: Annotated[str, Depends(require_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Grant/revoke platform-operator (impersonation) status. Only a platform
    admin may call this — an operator cannot self-grant or grant others operator
    rights."""
    service = AuthService(session)
    user = await service.set_platform_operator(user_id, body.is_platform_operator)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await PgAuditRepository(session).save(AuditEntry(
        action=AuditAction.PLATFORM_OPERATOR_GRANTED if body.is_platform_operator else AuditAction.PLATFORM_OPERATOR_REVOKED,
        entity_type="user",
        entity_id=user_id,
        actor=actor,
        details={"target_username": user.username},
        tenant_id=user.tenant_id,
    ))
    return _to_user_response(user)


@router.post("/users/{user_id}/impersonate", response_model=ImpersonationTokenResponse)
async def start_impersonation(
    user_id: str,
    request: Request,
    _actor: Annotated[str, Depends(require_platform_operator)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    service = ImpersonationService(session)
    try:
        result = await service.start(
            operator_user_id=request.state.user_id,
            operator_username=request.state.user,
            target_user_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ImpersonationTokenResponse(**result)


@router.post("/impersonate/stop")
async def stop_impersonation(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Ends THIS impersonation session's token (revokes exactly this jti) — the
    operator's own real token, if resumed separately, is untouched."""
    impersonated_by = getattr(request.state, "impersonated_by", None)
    if not impersonated_by:
        raise HTTPException(status_code=400, detail="This token is not an impersonation session")
    service = ImpersonationService(session)
    await service.stop(
        jti=request.state.jti,
        token_exp=request.state.token_exp,
        operator_user_id=impersonated_by,
        target_user_id=request.state.user_id,
        target_username=request.state.user,
        target_tenant_id=request.state.tenant_id,
    )
    return {"status": "ok"}


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
async def enroll_mfa(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    service = MfaService(session)
    result = await service.start_enrollment(request.state.user_id, request.state.user)
    return MfaEnrollResponse(**result)


@router.post("/mfa/confirm", response_model=MfaConfirmResponse)
async def confirm_mfa(
    body: MfaConfirmRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    service = MfaService(session)
    try:
        codes = await service.confirm_enrollment(request.state.user_id, body.code)
    except MfaLockedError as e:
        raise _mfa_locked_exception(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await PgAuditRepository(session).save(AuditEntry(
        action=AuditAction.MFA_ENABLED,
        entity_type="user",
        entity_id=request.state.user_id,
        actor=request.state.user,
        details={},
        tenant_id=getattr(request.state, "tenant_id", "default"),
    ))
    from app.infrastructure.auth.principal import invalidate_principal

    await session.commit()  # before invalidating, or the old state gets re-cached
    invalidate_principal(request.state.user_id)  # lifts a forced-enrolment restriction now
    return MfaConfirmResponse(recovery_codes=codes)


@router.post("/mfa/disable")
async def disable_mfa(
    body: MfaDisableRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from app.infrastructure.auth.principal import mfa_required_for

    role = (getattr(request.state, "roles", None) or [""])[0]
    privileged = bool(
        getattr(request.state, "is_platform_admin", False)
        or getattr(request.state, "is_platform_operator", False)
    )
    if mfa_required_for(role, privileged):
        raise HTTPException(
            status_code=403, detail="Two-factor authentication is required for your role"
        )
    service = MfaService(session)
    try:
        await service.disable(request.state.user_id, body.code)
    except MfaLockedError as e:
        raise _mfa_locked_exception(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await PgAuditRepository(session).save(AuditEntry(
        action=AuditAction.MFA_DISABLED,
        entity_type="user",
        entity_id=request.state.user_id,
        actor=request.state.user,
        details={},
        tenant_id=getattr(request.state, "tenant_id", "default"),
    ))
    return {"status": "ok"}
