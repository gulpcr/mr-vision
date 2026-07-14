"""Time-limited, audited impersonation for support/operator use.

An operator (is_platform_operator or is_platform_admin) can "become" another
user for a short window to see exactly what that user sees — the minted token
carries the TARGET user's own role/tenant/permissions, never the operator's;
impersonation never escalates privilege. Every start and stop is audited.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditAction
from app.domain.models import AuditEntry

IMPERSONATION_TOKEN_MINUTES = 30


class ImpersonationService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def start(
        self, operator_user_id: str, operator_username: str, target_user_id: str
    ) -> dict[str, Any]:
        from app.application.auth_service import AuthService
        from app.infrastructure.database.repositories import PgAuditRepository

        auth_service = AuthService(self._session)
        target = await auth_service.get_user_by_id(target_user_id)
        if target is None:
            raise ValueError(f"User '{target_user_id}' not found")
        if not target.is_active:
            raise ValueError("Cannot impersonate a deactivated user")

        token = auth_service.create_access_token(
            subject=target.id,
            username=target.username,
            role=target.role,
            tenant_id=target.tenant_id,
            is_platform_admin=False,
            is_platform_operator=False,
            impersonated_by=operator_user_id,
            expires_minutes=IMPERSONATION_TOKEN_MINUTES,
        )

        await PgAuditRepository(self._session).save(AuditEntry(
            action=AuditAction.IMPERSONATION_STARTED,
            entity_type="user",
            entity_id=target.id,
            actor=operator_username,
            details={"operator_id": operator_user_id, "target_username": target.username},
            tenant_id=target.tenant_id,
        ))
        await self._session.commit()

        return {
            "access_token": token,
            "token_type": "bearer",
            "user_id": target.id,
            "username": target.username,
            "role": target.role,
            "tenant_id": target.tenant_id,
            "expires_in_minutes": IMPERSONATION_TOKEN_MINUTES,
        }

    async def stop(
        self,
        jti: str,
        token_exp: int,
        operator_user_id: str,
        target_user_id: str,
        target_username: str,
        target_tenant_id: str,
    ) -> None:
        from app.application.auth_service import AuthService
        from app.infrastructure.database.repositories import PgAuditRepository
        from app.infrastructure.ratelimit.impersonation_blocklist import block_jti

        ttl_seconds = max(0, token_exp - int(time.time()))
        await block_jti(jti, ttl_seconds)

        operator = await AuthService(self._session).get_user_by_id(operator_user_id)
        operator_username = operator.username if operator else operator_user_id

        await PgAuditRepository(self._session).save(AuditEntry(
            action=AuditAction.IMPERSONATION_STOPPED,
            entity_type="user",
            entity_id=target_user_id,
            actor=operator_username,
            details={"operator_id": operator_user_id, "target_username": target_username},
            tenant_id=target_tenant_id,
        ))
        await self._session.commit()
