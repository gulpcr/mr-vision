from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.config import derive_secret, get_settings
from app.domain.enums import AuditAction
from app.domain.models import AuditEntry, User

logger = structlog.get_logger(__name__)


class TenantAccessError(Exception):
    """Raised on login when the user's tenant workspace is suspended/offboarded."""


def derive_tenant_jwt_secret(tenant_id: str) -> str:
    """Derive a tenant-specific JWT signing key from the platform master secret.

    No distinct secret is stored per tenant — no new table, no per-tenant
    secret to provision/rotate out of band. settings.jwt_secret_key is
    therefore the platform MASTER secret; no token is ever signed with it
    directly.
    """
    return derive_secret(f"jwt:{tenant_id}")


class AuthService:
    """Handles user authentication and token management."""

    def __init__(self, session):
        self._session = session

    async def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Verify credentials and return JWT tokens.

        Raises TenantAccessError (not a plain credentials failure) if the
        password is correct but the account's tenant workspace is suspended
        or offboarded — otherwise a suspended tenant's user could still obtain
        a token that only fails later on first API call.
        """
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        stmt = select(UserRecord).where(
            UserRecord.username == username,
            UserRecord.is_active == True,
        )
        result = await self._session.execute(stmt)
        user_record = result.scalar_one_or_none()

        if not user_record:
            return None

        if not self._verify_password(password, user_record.hashed_password):
            return None

        await self._check_tenant_access(user_record.tenant_id)

        if user_record.totp_enabled:
            # Password alone is not enough — hand back a short-lived,
            # narrowly-scoped token that can only be used at /mfa/verify, not
            # a real access token. The real one is minted by complete_mfa_login()
            # only after a valid TOTP/recovery code is presented.
            return {
                "mfa_required": True,
                "mfa_token": self._create_mfa_pending_token(user_record.id, user_record.tenant_id),
            }

        return await self._issue_tokens(user_record)

    async def complete_mfa_login(self, mfa_token: str, code: str) -> dict[str, Any] | None:
        """Second step of login when MFA is enabled: verify the mfa_pending
        token plus a TOTP/recovery code, then issue the real access token.
        """
        from app.application.mfa_service import MfaService

        payload = self.decode_token(mfa_token)
        if not payload or payload.get("purpose") != "mfa_pending":
            return None

        user = await self.get_user_by_id(payload.get("sub", ""))
        if not user or not user.is_active:
            return None

        if not await MfaService(self._session).verify_code(user.id, code):
            return None

        return await self._issue_tokens(user)

    async def _issue_tokens(self, user) -> dict[str, Any]:
        """Accepts either a UserRecord (from authenticate) or a domain User
        (from complete_mfa_login) — both expose the same fields used here."""
        from app.infrastructure.database.repositories import PgAuditRepository

        access_token = self.create_access_token(
            subject=user.id,
            username=user.username,
            role=user.role,
            tenant_id=user.tenant_id,
            is_platform_admin=user.is_platform_admin,
            is_platform_operator=user.is_platform_operator,
        )
        await PgAuditRepository(self._session).save(AuditEntry(
            action=AuditAction.USER_LOGIN,
            entity_type="user",
            entity_id=user.id,
            actor=user.username,
            details={},
            tenant_id=user.tenant_id,
        ))
        return {
            "mfa_required": False,
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "tenant_id": user.tenant_id,
        }

    def _create_mfa_pending_token(self, user_id: str, tenant_id: str) -> str:
        from jose import jwt

        settings = get_settings()
        expire = datetime.now(timezone.utc) + timedelta(minutes=5)
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "purpose": "mfa_pending",
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
        tenant_secret = derive_tenant_jwt_secret(tenant_id)
        return jwt.encode(payload, tenant_secret, algorithm=settings.jwt_algorithm)

    async def _check_tenant_access(self, tenant_id: str) -> None:
        from app.infrastructure.database.models import TenantRecord
        from sqlalchemy import select

        result = await self._session.execute(
            select(TenantRecord).where(TenantRecord.id == tenant_id)
        )
        tenant_record = result.scalar_one_or_none()
        if tenant_record is None:
            return
        if tenant_record.status == "suspended":
            raise TenantAccessError(
                "This workspace has been suspended. Contact your workspace "
                "administrator or support@mr-vision.ai to restore access."
            )
        if tenant_record.status == "offboarded":
            raise TenantAccessError(
                "This workspace has been offboarded and is no longer accessible. "
                "Contact support@mr-vision.ai if you believe this is an error."
            )

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        full_name: str = "",
        role: str = "viewer",
        tenant_id: str = "default",
        is_platform_admin: bool = False,
        is_platform_operator: bool = False,
    ) -> User:
        """Create a new user account."""
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        # Check uniqueness
        existing = await self._session.execute(
            select(UserRecord).where(
                (UserRecord.username == username) | (UserRecord.email == email)
            )
        )
        if existing.scalar_one_or_none():
            raise ValueError("Username or email already exists")

        hashed = self._hash_password(password)
        user_id = str(uuid.uuid4())

        record = UserRecord(
            id=user_id,
            username=username,
            email=email,
            hashed_password=hashed,
            full_name=full_name,
            role=role,
            tenant_id=tenant_id,
            is_active=True,
            is_platform_admin=is_platform_admin,
            is_platform_operator=is_platform_operator,
        )
        self._session.add(record)
        await self._session.flush()

        from app.infrastructure.database.repositories import PgAuditRepository
        await PgAuditRepository(self._session).save(AuditEntry(
            action=AuditAction.USER_CREATED,
            entity_type="user",
            entity_id=user_id,
            actor="system",
            details={"username": username, "role": role},
            tenant_id=tenant_id,
        ))

        return User(
            id=user_id,
            username=username,
            email=email,
            hashed_password=hashed,
            full_name=full_name,
            role=role,
            tenant_id=tenant_id,
            is_platform_admin=is_platform_admin,
            is_platform_operator=is_platform_operator,
        )

    async def list_users(self, tenant_id: str | None = None) -> list[User]:
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        stmt = select(UserRecord).order_by(UserRecord.created_at.desc())
        if tenant_id:
            stmt = stmt.where(UserRecord.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        users = []
        for r in result.scalars().all():
            users.append(User(
                id=r.id,
                username=r.username,
                email=r.email,
                hashed_password="",
                full_name=r.full_name,
                role=r.role,
                tenant_id=r.tenant_id,
                is_active=r.is_active,
                is_platform_admin=r.is_platform_admin,
                is_platform_operator=r.is_platform_operator,
                totp_enabled=r.totp_enabled,
                created_at=r.created_at,
                updated_at=r.updated_at,
            ))
        return users

    async def get_user_by_id(self, user_id: str) -> User | None:
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        stmt = select(UserRecord).where(UserRecord.id == user_id)
        result = await self._session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r:
            return None
        return User(
            id=r.id,
            username=r.username,
            email=r.email,
            hashed_password="",
            full_name=r.full_name,
            role=r.role,
            tenant_id=r.tenant_id,
            is_active=r.is_active,
            is_platform_admin=r.is_platform_admin,
            is_platform_operator=r.is_platform_operator,
            totp_enabled=r.totp_enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )

    async def update_user_role(self, user_id: str, role: str) -> User | None:
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select, update

        stmt = update(UserRecord).where(UserRecord.id == user_id).values(role=role)
        await self._session.execute(stmt)
        await self._session.flush()
        return await self.get_user_by_id(user_id)

    async def deactivate_user(self, user_id: str) -> bool:
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import update

        stmt = update(UserRecord).where(UserRecord.id == user_id).values(is_active=False)
        await self._session.execute(stmt)
        await self._session.flush()
        return True

    async def set_platform_admin(self, user_id: str, is_platform_admin: bool) -> User | None:
        """Bootstrap/revoke platform-admin status on an existing user.

        No self-service path grants this — it's set directly against the DB
        (or by an existing platform admin, once one exists) by design.
        """
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import update

        stmt = (
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(is_platform_admin=is_platform_admin)
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return await self.get_user_by_id(user_id)

    async def set_platform_operator(self, user_id: str, is_platform_operator: bool) -> User | None:
        """Grant/revoke platform-operator status (impersonation rights only —
        not tenant lifecycle, admin promotion, or all-tenant data resets,
        which stay is_platform_admin-only). Only a platform admin can call
        this (see require_platform_admin on the wiring endpoint).
        """
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import update

        stmt = (
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(is_platform_operator=is_platform_operator)
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return await self.get_user_by_id(user_id)

    def decode_token(self, token: str) -> dict[str, Any] | None:
        """Decode and validate a JWT token.

        Tokens are signed with a per-tenant derived key (see
        derive_tenant_jwt_secret), not the master secret directly, so the
        tenant_id claim must be read WITHOUT verifying the signature first —
        that's the only way to know which key to verify against. The
        subsequent jwt.decode() call is the real, fully-verified check
        (signature + algorithm allowlist + expiry); the unverified peek below
        carries no security weight of its own.
        """
        try:
            from jose import jwt
            settings = get_settings()

            unverified = jwt.get_unverified_claims(token)
            tenant_id = unverified.get("tenant_id")
            if not tenant_id:
                return None
            tenant_secret = derive_tenant_jwt_secret(tenant_id)

            payload = jwt.decode(
                token,
                tenant_secret,
                algorithms=[settings.jwt_algorithm],
            )
            return payload
        except Exception:
            return None

    def create_access_token(
        self,
        subject: str,
        username: str,
        role: str,
        tenant_id: str,
        is_platform_admin: bool = False,
        is_platform_operator: bool = False,
        impersonated_by: str | None = None,
        expires_minutes: int | None = None,
    ) -> str:
        """Mint a signed access token.

        impersonated_by / expires_minutes are used by ImpersonationService to
        mint a short-lived token representing another user — impersonation
        tokens never carry is_platform_admin/is_platform_operator themselves
        (the operator borrows the target's own permissions, never gains
        elevated ones by impersonating), and get their own jti so the
        impersonate/stop endpoint can revoke exactly that token via the
        Redis blocklist without touching the operator's real session.
        """
        from jose import jwt
        settings = get_settings()
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=expires_minutes or settings.jwt_access_token_expire_minutes
        )
        payload = {
            "sub": subject,
            "username": username,
            "role": role,
            "tenant_id": tenant_id,
            "is_platform_admin": is_platform_admin,
            "is_platform_operator": is_platform_operator,
            "jti": str(uuid.uuid4()),
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
        if impersonated_by:
            payload["impersonated_by"] = impersonated_by
        tenant_secret = derive_tenant_jwt_secret(tenant_id)
        return jwt.encode(payload, tenant_secret, algorithm=settings.jwt_algorithm)

    # Password hashing uses pbkdf2_sha256 (pure-Python via passlib/hashlib).
    # NOTE: bcrypt is intentionally NOT used — passlib 1.7.4 is incompatible with
    # bcrypt >= 4.1 (removed __about__), which broke hashing/verification in this
    # image. pbkdf2_sha256 is a strong, dependency-light scheme with no native lib.
    @staticmethod
    def _pwd_context():
        from passlib.context import CryptContext
        return CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

    @staticmethod
    def _hash_password(password: str) -> str:
        return AuthService._pwd_context().hash(password)

    @staticmethod
    def _verify_password(plain: str, hashed: str) -> bool:
        try:
            return AuthService._pwd_context().verify(plain, hashed)
        except Exception:
            # Unidentifiable / legacy hash → treat as failed auth, never 500.
            return False
