"""Practitioner / PractitionerRole / Organization API (FHIR step 7).

GET   /practitioners/{user_id}                 require(user.manage)  identity + roles
PATCH /practitioners/{user_id}                 require(user.manage)  NPI / licence + HumanName
POST  /practitioners/{user_id}/roles           require(user.manage)  grant a role
DELETE /practitioners/{user_id}/roles/{name}   require(user.manage)  end a role's period
GET   /organization                            require(study.view)   this tenant as Organization
PATCH /organization                            require(config.manage) identifier / type / address

Gated on existing permissions rather than new ones: granting a clinical role is the same
authority as managing users, and organization identity is deployment configuration.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.practitioner_service import (
    PractitionerService,
    PractitionerValidationError,
)
from app.interface.api.dependencies import get_session
from app.interface.middleware.auth import require_permission

router = APIRouter(tags=["practitioners"])


class PractitionerUpdate(BaseModel):
    # The professional identifier — an NPI, state licence, or equivalent. Must come with
    # its system: an unqualified identifier cannot be matched by a receiving system.
    identifier_system: str | None = Field(None, max_length=128)
    identifier_value: str | None = Field(None, max_length=128)
    family_name: str | None = Field(None, max_length=128)
    # Accepts "John A" or ["John", "A"] — given + middle, per FHIR HumanName.given[].
    given_names: Any | None = None
    name_prefix: str | None = Field(None, max_length=32)
    name_suffix: str | None = Field(None, max_length=32)


class RoleGrant(BaseModel):
    role_name: str = Field(..., min_length=1, max_length=64)
    role_code_system: str | None = Field(None, max_length=128)
    role_code: str | None = Field(None, max_length=64)
    specialty_code_system: str | None = Field(None, max_length=128)
    specialty_code: str | None = Field(None, max_length=64)
    make_primary: bool = False


class OrganizationUpdate(BaseModel):
    identifier_system: str | None = Field(None, max_length=128)
    identifier_value: str | None = Field(None, max_length=128)
    org_type: str | None = Field(None, max_length=32)
    # FHIR Address object: {line: [...], city, postalCode, country}.
    address: dict[str, Any] | None = None


def _tenant(request: Request) -> str:
    return getattr(request.state, "tenant_id", "default") or "default"


@router.get("/practitioners/{user_id}", dependencies=[require_permission("user.manage")])
async def get_practitioner(
    user_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await PractitionerService(session).get_practitioner(user_id, _tenant(request))
    if result is None:
        raise HTTPException(404, "Practitioner not found")
    return result


@router.patch("/practitioners/{user_id}", dependencies=[require_permission("user.manage")])
async def update_practitioner(
    user_id: str,
    body: PractitionerUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        result = await PractitionerService(session).update_practitioner(
            user_id, body.model_dump(exclude_unset=True), _tenant(request)
        )
    except PractitionerValidationError as e:
        raise HTTPException(422, str(e))
    if result is None:
        raise HTTPException(404, "Practitioner not found")
    await session.commit()
    return result


@router.post(
    "/practitioners/{user_id}/roles",
    status_code=201,
    dependencies=[require_permission("user.manage")],
)
async def grant_role(
    user_id: str,
    body: RoleGrant,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        result = await PractitionerService(session).add_role(
            user_id,
            body.role_name,
            tenant_id=_tenant(request),
            role_code_system=body.role_code_system,
            role_code=body.role_code,
            specialty_code_system=body.specialty_code_system,
            specialty_code=body.specialty_code,
            make_primary=body.make_primary,
        )
    except PractitionerValidationError as e:
        raise HTTPException(422, str(e))
    await session.commit()
    return result


@router.delete(
    "/practitioners/{user_id}/roles/{role_name}",
    dependencies=[require_permission("user.manage")],
)
async def revoke_role(
    user_id: str,
    role_name: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """End the role's validity period; the row is kept so history stays interpretable."""
    try:
        ok = await PractitionerService(session).revoke_role(
            user_id, role_name, _tenant(request)
        )
    except PractitionerValidationError as e:
        raise HTTPException(422, str(e))
    if not ok:
        raise HTTPException(404, "Role not held by this practitioner")
    await session.commit()
    return {"status": "ok", "role_name": role_name, "revoked": True}


@router.get("/organization", dependencies=[require_permission("study.view")])
async def get_organization(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await PractitionerService(session).get_organization(_tenant(request))
    if result is None:
        raise HTTPException(404, "Organization not found")
    return result


@router.patch("/organization", dependencies=[require_permission("config.manage")])
async def update_organization(
    body: OrganizationUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        result = await PractitionerService(session).update_organization(
            _tenant(request), body.model_dump(exclude_unset=True)
        )
    except PractitionerValidationError as e:
        raise HTTPException(422, str(e))
    if result is None:
        raise HTTPException(404, "Organization not found")
    await session.commit()
    return result
