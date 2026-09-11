from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth_service import AuthService, TenantAccessError
from app.application.impersonation_service import ImpersonationService
from app.application.mfa_service import MfaLockedError, MfaService
from app.domain.enums import AuditAction
from app.domain.models import AuditEntry
from app.infrastructure.database.repositories import PgAuditRepository
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import (
    require_permission,
    require_platform_admin,
    require_platform_operator,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)


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
    )


def _mfa_locked_exception(e: MfaLockedError) -> HTTPException:
    return HTTPException(
        status_code=423,
        detail=f"Too many failed attempts. Try again in {e.retry_after_seconds} seconds.",
        headers={"Retry-After": str(e.retry_after_seconds)},
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Authenticate user and return a JWT token, or an mfa_token if MFA is enabled."""
    service = AuthService(session)
    try:
        result = await service.authenticate(body.username, body.password)
    except TenantAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not result:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return LoginResponse(**result)


@router.post("/mfa/verify", response_model=LoginResponse)
async def verify_mfa(
    body: MfaVerifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Second step of login when the account has MFA enabled."""
    service = AuthService(session)
    try:
        result = await service.complete_mfa_login(body.mfa_token, body.code)
    except MfaLockedError as e:
        raise _mfa_locked_exception(e)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA session, or wrong code")
    return LoginResponse(**result)


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
    """Register a new user (default role: viewer)."""
    service = AuthService(session)
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
    if tenant_id and tenant_id != caller_tenant_id and not getattr(request.state, "is_platform_admin", False):
        raise HTTPException(403, "Viewing another tenant's users requires platform admin access")
    service = AuthService(session)
    users = await service.list_users(tenant_id=tenant_id or caller_tenant_id)
    return [_to_user_response(u) for u in users]


@router.put("/users/{user_id}/role", dependencies=[require_permission("user.manage")])
async def update_user_role(
    user_id: str,
    role: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Update a user's role (requires user.manage)."""
    service = AuthService(session)
    user = await service.update_user_role(user_id, role)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "ok", "user_id": user_id, "role": role}


@router.delete("/users/{user_id}", dependencies=[require_permission("user.manage")])
async def deactivate_user(
    user_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Deactivate a user account (requires user.manage)."""
    service = AuthService(session)
    await service.deactivate_user(user_id)
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
    return MfaConfirmResponse(recovery_codes=codes)


@router.post("/mfa/disable")
async def disable_mfa(
    body: MfaDisableRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
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
