from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth_service import AuthService
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=128)
    email: str = Field(..., max_length=256)
    password: str = Field(..., min_length=8)
    full_name: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    role: str
    tenant_id: str


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    full_name: str
    role: str
    tenant_id: str
    is_active: bool
    created_at: str | None = None


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Authenticate user and return JWT token."""
    service = AuthService(session)
    result = await service.authenticate(body.username, body.password)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(**result)


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

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        tenant_id=user.tenant_id,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.get("/users", response_model=list[UserResponse], dependencies=[require_permission("user.manage")])
async def list_users(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """List all users (requires user.manage)."""
    service = AuthService(session)
    users = await service.list_users()
    return [
        UserResponse(
            id=u.id,
            username=u.username,
            email=u.email,
            full_name=u.full_name,
            role=u.role,
            tenant_id=u.tenant_id,
            is_active=u.is_active,
            created_at=u.created_at.isoformat() if u.created_at else None,
        )
        for u in users
    ]


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
