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


class AmbiguousAccountError(Exception):
    """Raised on login when the username exists in several workspaces and none was
    given — usernames are unique per tenant, not platform-wide."""


class InvitationError(ValueError):
    """Invalid, expired or already-used invitation / reset token."""


class AccountLockedError(Exception):
    """Too many failed passwords for this account; retry after ``retry_after`` s."""

    def __init__(self, retry_after: int):
        super().__init__("Too many failed sign-in attempts — try again later")
        self.retry_after = retry_after


class PasswordPolicyError(ValueError):
    """The new password does not meet the policy; ``problems`` lists why."""

    def __init__(self, problems: list[str]):
        super().__init__(" ".join(problems))
        self.problems = problems


class SeatLimitError(ValueError):
    """The tenant has reached its plan's user limit."""


PLATFORM_TENANT_ID = "default"


def _invitation_ttl() -> timedelta:
    """How long an invitation / password-reset link stays valid (INVITATION_TTL_HOURS)."""
    return timedelta(hours=max(1, get_settings().invitation_ttl_hours))


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def derive_tenant_jwt_secret(tenant_id: str) -> str:
    """Derive a tenant-specific JWT signing key from the platform master secret.

    No distinct secret is stored per tenant — no new table, no per-tenant secret to
    provision/rotate out of band. settings.jwt_secret_key is therefore the platform
    MASTER secret; no token is ever signed with it directly.
    """
    return derive_secret(f"jwt:{tenant_id}")


class AuthService:
    """Handles user authentication and token management."""

    def __init__(self, session):
        self._session = session

    async def authenticate(
        self, username: str, password: str, tenant_id: str | None = None,
        client_ip: str = "",
    ) -> dict[str, Any] | None:
        """Verify credentials and return JWT tokens.

        Raises TenantAccessError (not a plain credentials failure) if the password is
        correct but the account's tenant workspace is suspended or offboarded —
        otherwise a suspended tenant's user could still obtain a token that only fails
        later on first API call.
        """
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        stmt = select(UserRecord).where(
            UserRecord.username == username,
            UserRecord.is_active == True,
        )
        if tenant_id:
            stmt = stmt.where(UserRecord.tenant_id == tenant_id)
        candidates = (await self._session.execute(stmt)).scalars().all()
        if len(candidates) > 1:
            # The same username exists in several workspaces and no workspace was
            # given (subdomain / X-Tenant-Slug / login form) — never guess.
            raise AmbiguousAccountError(
                "This username exists in more than one workspace — enter your workspace"
            )
        user_record = candidates[0] if candidates else None

        from app.infrastructure.ratelimit import login_lockout

        lock_tenant = user_record.tenant_id if user_record else tenant_id
        locked, retry_after = await login_lockout.is_locked(lock_tenant, username)
        if locked:
            await self._audit_login_failure(user_record, username, lock_tenant, client_ip, "locked")
            raise AccountLockedError(retry_after)

        if not user_record or not self._verify_password(password, user_record.hashed_password):
            reason = "bad_password" if user_record else "unknown_user"
            count = await login_lockout.record_failure(lock_tenant, username)
            await self._audit_login_failure(user_record, username, lock_tenant, client_ip, reason)
            if count and count >= get_settings().login_max_attempts:
                await self._audit(
                    AuditAction.ACCOUNT_LOCKED.value,
                    user_record.id if user_record else username,
                    username, lock_tenant or PLATFORM_TENANT_ID,
                    {"client_ip": client_ip, "failed_attempts": count},
                )
            return None

        await self._check_tenant_access(user_record.tenant_id)
        await login_lockout.clear_failures(lock_tenant, username)

        if user_record.totp_enabled:
            # Password alone is not enough — hand back a short-lived, narrowly-scoped
            # token that can only be used at /mfa/verify, not a real access token. The
            # real one is minted by complete_mfa_login() only after a valid
            # TOTP/recovery code is presented.
            return {
                "mfa_required": True,
                "mfa_token": self._create_mfa_pending_token(user_record.id, user_record.tenant_id),
            }

        return await self._issue_tokens(user_record)

    async def complete_mfa_login(self, mfa_token: str, code: str) -> dict[str, Any] | None:
        """Second step of login when MFA is enabled: verify the mfa_pending token
        plus a TOTP/recovery code, then issue the real access token."""
        from app.application.mfa_service import MfaService

        payload = self.decode_token(mfa_token)
        if not payload or payload.get("purpose") != "mfa_pending":
            return None

        user = await self.get_user_by_id(payload.get("sub", ""))
        if not user or not user.is_active:
            return None

        if not await MfaService(self._session).verify_code(user.id, code):
            await self._audit(
                AuditAction.MFA_FAILED.value, user.id, user.username, user.tenant_id, {}
            )
            return None

        return await self._issue_tokens(user)

    async def _issue_tokens(self, user, audit_login: bool = True) -> dict[str, Any]:
        """Accepts either a UserRecord (from authenticate) or a domain User (from
        complete_mfa_login) — both expose the same fields used here."""
        from app.infrastructure.database.models import UserRecord
        from app.infrastructure.database.repositories import PgAuditRepository
        from sqlalchemy import update

        in_platform_tenant = user.tenant_id == PLATFORM_TENANT_ID
        access_token = self.create_access_token(
            subject=user.id,
            username=user.username,
            role=user.role,
            tenant_id=user.tenant_id,
            # Platform flags are honoured only for Admin-tenant accounts.
            is_platform_admin=bool(user.is_platform_admin) and in_platform_tenant,
            is_platform_operator=bool(user.is_platform_operator) and in_platform_tenant,
            token_version=getattr(user, "token_version", 0) or 0,
        )
        if audit_login:
            await self._session.execute(
                update(UserRecord)
                .where(UserRecord.id == user.id)
                .values(last_login_at=datetime.now(timezone.utc))
            )
            await PgAuditRepository(self._session).save(AuditEntry(
                action=AuditAction.USER_LOGIN,
                entity_type="user",
                entity_id=user.id,
                actor=user.username,
                details={},
                tenant_id=user.tenant_id,
            ))
        from app.infrastructure.auth.principal import mfa_required_for

        return {
            "mfa_required": False,
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "tenant_id": user.tenant_id,
            "token_version": getattr(user, "token_version", 0) or 0,
            # Sign-in restrictions the UI must resolve before anything else.
            "must_change_password": bool(getattr(user, "must_change_password", False)),
            "mfa_enrollment_required": (
                not getattr(user, "totp_enabled", False)
                and mfa_required_for(
                    user.role,
                    bool(user.is_platform_admin or user.is_platform_operator)
                    and in_platform_tenant,
                )
            ),
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
                "administrator for access."
            )
        if tenant_record.status == "offboarded":
            raise TenantAccessError(
                "This workspace has been offboarded and is no longer accessible."
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

        # Unique per tenant (alembic 046) — the same username may exist elsewhere.
        existing = await self._session.execute(
            select(UserRecord).where(
                UserRecord.tenant_id == tenant_id,
                (UserRecord.username == username) | (UserRecord.email == email),
            )
        )
        if existing.scalars().first():
            raise ValueError("Username or email already exists in this workspace")

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

    # ── Invitations, password resets, session revocation ──────────────────────

    async def invite_user(
        self,
        tenant_id: str,
        username: str,
        email: str,
        role: str,
        full_name: str = "",
        invited_by: str = "system",
    ) -> tuple[User, str]:
        """Create an ``invited`` account in ``tenant_id`` and return it with the one-time
        cleartext invitation token (only its SHA-256 is stored; valid INVITATION_TTL_HOURS, default 3). The
        account cannot log in until the invitation is accepted and a password is set."""
        import secrets

        from app.infrastructure.database.models import RoleRecord, TenantRecord, UserRecord
        from sqlalchemy import func, select

        role_exists = (
            await self._session.execute(
                select(RoleRecord.id).where(RoleRecord.tenant_id == tenant_id, RoleRecord.name == role)
            )
        ).first()
        if role_exists is None:
            raise ValueError(f"Role '{role}' is not defined for this workspace")

        tenant = (
            await self._session.execute(select(TenantRecord).where(TenantRecord.id == tenant_id))
        ).scalar_one_or_none()
        if tenant is not None and tenant.max_users:
            seats = (
                await self._session.execute(
                    select(func.count()).select_from(UserRecord).where(UserRecord.tenant_id == tenant_id)
                )
            ).scalar_one()
            if seats >= tenant.max_users:
                raise SeatLimitError(f"This workspace has reached its limit of {tenant.max_users} users")

        user = await self.create_user(
            username=username,
            email=email,
            password=secrets.token_urlsafe(32),  # unusable until the invite is accepted
            full_name=full_name,
            role=role,
            tenant_id=tenant_id,
        )
        token = await self._issue_one_time_token(user.id, tenant_id, status="invited")
        await self._audit("user_invited", user.id, invited_by, tenant_id,
                          {"username": username, "role": role})
        user.status = "invited"
        return user, token

    async def issue_password_reset(self, user_id: str, tenant_id: str, actor: str) -> str:
        """One-time token letting the user set a new password (same flow as an invite)."""
        token = await self._issue_one_time_token(user_id, tenant_id, status=None)
        await self._audit("password_reset_issued", user_id, actor, tenant_id, {})
        return token

    async def _issue_one_time_token(self, user_id: str, tenant_id: str, status: str | None) -> str:
        import secrets

        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import update

        token = secrets.token_urlsafe(32)
        values: dict[str, Any] = {
            "invitation_token_hash": _hash_token(token),
            "invitation_expires_at": datetime.now(timezone.utc) + _invitation_ttl(),
        }
        if status:
            values["status"] = status
        result = await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id, UserRecord.tenant_id == tenant_id)
            .values(**values)
        )
        if not result.rowcount:
            raise ValueError("User not found")
        await self._session.flush()
        return token

    async def accept_invitation(self, token: str, password: str) -> User:
        """Redeem an invitation or reset token: set the password, activate the account,
        burn the token, and revoke any previously issued sessions."""
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        record = (
            await self._session.execute(
                select(UserRecord).where(UserRecord.invitation_token_hash == _hash_token(token))
            )
        ).scalar_one_or_none()
        if record is None:
            raise InvitationError("This link is invalid or has already been used")
        expires = record.invitation_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is None or expires < datetime.now(timezone.utc):
            raise InvitationError("This link has expired — ask your administrator for a new one")
        await self._check_tenant_access(record.tenant_id)
        self.check_password_policy(password, record.username)

        record.hashed_password = self._hash_password(password)
        record.must_change_password = False
        record.password_changed_at = datetime.now(timezone.utc)
        record.status = "active"
        record.is_active = True
        record.invitation_token_hash = None
        record.invitation_expires_at = None
        record.token_version = (record.token_version or 0) + 1
        await self._session.flush()
        await self._audit("invitation_accepted", record.id, record.username, record.tenant_id, {})
        return await self.get_user_by_id(record.id)

    @staticmethod
    def check_password_policy(password: str, username: str = "") -> None:
        from app.domain.password_policy import password_problems

        problems = password_problems(password, username, get_settings().password_min_length)
        if problems:
            raise PasswordPolicyError(problems)

    async def change_password(
        self, user_id: str, tenant_id: str, current_password: str, new_password: str
    ) -> dict[str, Any] | None:
        """Self-service password change. Returns fresh tokens (every other session is
        revoked), or None if the current password is wrong."""
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select

        record = (
            await self._session.execute(
                select(UserRecord).where(
                    UserRecord.id == user_id, UserRecord.tenant_id == tenant_id,
                    UserRecord.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if record is None or not self._verify_password(current_password, record.hashed_password):
            return None
        if self._verify_password(new_password, record.hashed_password):
            raise PasswordPolicyError(["Choose a password different from your current one."])
        self.check_password_policy(new_password, record.username)

        record.hashed_password = self._hash_password(new_password)
        record.must_change_password = False
        record.password_changed_at = datetime.now(timezone.utc)
        record.token_version = (record.token_version or 0) + 1
        await self._session.flush()
        await self._audit(
            AuditAction.PASSWORD_CHANGED.value, record.id, record.username, record.tenant_id, {}
        )
        return await self._issue_tokens(record, audit_login=False)

    async def audit_logout(self, user_id: str, username: str, tenant_id: str, client_ip: str) -> None:
        await self._audit(
            AuditAction.USER_LOGOUT.value, user_id, username, tenant_id, {"client_ip": client_ip}
        )

    async def _audit_login_failure(
        self, user_record, username: str, tenant_id: str | None, client_ip: str, reason: str
    ) -> None:
        # entity_id is the account when it exists, else the attempted username, so
        # repeated guessing against non-existent accounts is still reviewable.
        await self._audit(
            AuditAction.LOGIN_FAILED.value,
            user_record.id if user_record else username,
            username,
            (user_record.tenant_id if user_record else tenant_id) or PLATFORM_TENANT_ID,
            {"reason": reason, "client_ip": client_ip},
        )

    async def revoke_sessions(self, user_id: str, tenant_id: str, actor: str) -> bool:
        """Invalidate every access token issued to the user (bumps token_version)."""
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import update

        result = await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id, UserRecord.tenant_id == tenant_id)
            .values(token_version=UserRecord.token_version + 1)
        )
        await self._session.flush()
        if result.rowcount:
            await self._audit("sessions_revoked", user_id, actor, tenant_id, {})
        return bool(result.rowcount)

    async def _audit(self, action: str, user_id: str, actor: str, tenant_id: str, details: dict) -> None:
        from app.infrastructure.database.repositories import PgAuditRepository

        await PgAuditRepository(self._session, tenant_id=tenant_id).save(AuditEntry(
            action=action,
            entity_type="user",
            entity_id=user_id,
            actor=actor or "system",
            details=details,
            tenant_id=tenant_id,
        ))

    async def list_referring_doctors(self, tenant_id: str) -> list[dict[str, Any]]:
        """Active users of the workspace whose role is referral-scoped (the referring
        Doctor) — the choices for an order's referring physician at intake."""
        from app.domain.permissions import is_referral_scoped
        from app.infrastructure.database.models import RoleRecord, UserRecord
        from sqlalchemy import select

        roles = (await self._session.execute(
            select(RoleRecord).where(RoleRecord.tenant_id == tenant_id)
        )).scalars().all()
        doctor_roles = [r.name for r in roles if is_referral_scoped(r.permissions or [])]
        if not doctor_roles:
            return []
        rows = (await self._session.execute(
            select(UserRecord).where(
                UserRecord.tenant_id == tenant_id,
                UserRecord.role.in_(doctor_roles),
                UserRecord.is_active == True,  # noqa: E712
            ).order_by(UserRecord.full_name, UserRecord.username)
        )).scalars().all()
        return [{"id": u.id, "username": u.username, "full_name": u.full_name or u.username} for u in rows]

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
                status=r.status or "active",
                token_version=r.token_version or 0,
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
            status=r.status or "active",
            token_version=r.token_version or 0,
            must_change_password=bool(getattr(r, "must_change_password", False)),
            last_login_at=getattr(r, "last_login_at", None),
            created_at=r.created_at,
            updated_at=r.updated_at,
        )

    async def update_user_role(self, user_id: str, role: str, tenant_id: str) -> User | None:
        """Change a user's role within ``tenant_id``.

        Returns None when the user is not in that tenant (so a tenant admin can never
        touch another tenant's accounts). Raises ValueError when ``role`` is not a role
        defined for the tenant, or when the change would leave the tenant without an
        active admin.
        """
        from app.infrastructure.database.models import RoleRecord, UserRecord
        from sqlalchemy import select, update

        target = (
            await self._session.execute(
                select(UserRecord).where(UserRecord.id == user_id, UserRecord.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if target is None:
            return None

        role_exists = (
            await self._session.execute(
                select(RoleRecord.id).where(RoleRecord.tenant_id == tenant_id, RoleRecord.name == role)
            )
        ).first()
        if role_exists is None and role != "admin":
            raise ValueError(f"Role '{role}' is not defined for this workspace")

        if target.role == "admin" and role != "admin":
            await self._ensure_other_active_admin(tenant_id, excluding_user_id=user_id)

        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id, UserRecord.tenant_id == tenant_id)
            .values(role=role)
        )
        await self._session.flush()
        return await self.get_user_by_id(user_id)

    async def deactivate_user(self, user_id: str, tenant_id: str) -> User | None:
        """Deactivate a user within ``tenant_id``; None if not in that tenant. Raises
        ValueError if this would leave the tenant without an active admin."""
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import select, update

        target = (
            await self._session.execute(
                select(UserRecord).where(UserRecord.id == user_id, UserRecord.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if target is None:
            return None
        if target.role == "admin" and target.is_active:
            await self._ensure_other_active_admin(tenant_id, excluding_user_id=user_id)

        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id, UserRecord.tenant_id == tenant_id)
            .values(is_active=False)
        )
        await self._session.flush()
        return await self.get_user_by_id(user_id)

    async def _ensure_other_active_admin(self, tenant_id: str, excluding_user_id: str) -> None:
        from app.infrastructure.database.models import UserRecord
        from sqlalchemy import func, select

        remaining = (
            await self._session.execute(
                select(func.count()).select_from(UserRecord).where(
                    UserRecord.tenant_id == tenant_id,
                    UserRecord.role == "admin",
                    UserRecord.is_active == True,  # noqa: E712
                    UserRecord.id != excluding_user_id,
                )
            )
        ).scalar_one()
        if remaining == 0:
            raise ValueError("A workspace must keep at least one active admin")

    async def set_platform_admin(self, user_id: str, is_platform_admin: bool) -> User | None:
        """Bootstrap/revoke platform-admin status on an existing user.

        No self-service path grants this — it's set directly against the DB (or by an
        existing platform admin, once one exists) by design.
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
        """Grant/revoke platform-operator status (impersonation rights only — not
        tenant lifecycle, admin promotion, or all-tenant data resets, which stay
        is_platform_admin-only). Only a platform admin can call this (see
        require_platform_admin on the wiring endpoint).
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

    def create_viewer_token(
        self,
        subject: str,
        tenant_id: str,
        is_platform_admin: bool = False,
        expires_minutes: int | None = None,
        referral_scoped: bool = False,
        token_version: int = 0,
    ) -> str:
        """Mint a ``purpose=viewer`` token for the viewer-session cookie.

        ``referral_scoped`` (the referring Doctor) adds ``ref`` = the user's id: the
        viewer may then open only studies referred by that user.

        It authorises exactly two things — DICOMweb/WADO reads (checked by the nginx
        auth_request hook) and the realtime WebSocket — and is rejected by
        RBACMiddleware as an API bearer token, so a cookie that leaks or is replayed
        cross-site cannot drive the REST API.
        """
        from jose import jwt

        settings = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": subject,
            "tenant_id": tenant_id,
            "is_platform_admin": is_platform_admin,
            "purpose": "viewer",
            "ref": subject if referral_scoped else None,
            # Checked against the live account on every viewer request, so a revoked
            # session / password change / deactivation ends image access immediately.
            "tv": token_version,
            "iat": now,
            "exp": now + timedelta(
                minutes=expires_minutes or settings.jwt_access_token_expire_minutes
            ),
        }
        return jwt.encode(payload, derive_tenant_jwt_secret(tenant_id), algorithm=settings.jwt_algorithm)

    def decode_token(self, token: str) -> dict[str, Any] | None:
        """Decode and validate a JWT token.

        Tokens are signed with a per-tenant derived key (see derive_tenant_jwt_secret),
        not the master secret directly, so the tenant_id claim must be read WITHOUT
        verifying the signature first — that's the only way to know which key to
        verify against. The subsequent jwt.decode() call is the real, fully-verified
        check (signature + algorithm allowlist + expiry); the unverified peek below
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
        token_version: int = 0,
    ) -> str:
        """Mint a signed access token.

        ``token_version`` is embedded as ``tv``; RBACMiddleware rejects a token whose
        ``tv`` no longer matches the user's ``token_version`` (revocation).

        impersonated_by / expires_minutes are used by ImpersonationService to mint a
        short-lived token representing another user — impersonation tokens never
        carry is_platform_admin/is_platform_operator themselves (the operator borrows
        the target's own permissions, never gains elevated ones by impersonating), and
        get their own jti so the impersonate/stop endpoint can revoke exactly that
        token via the Redis blocklist without touching the operator's real session.
        """
        from jose import jwt
        settings = get_settings()
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=expires_minutes or settings.jwt_access_token_expire_minutes
        )
        payload = {
            "sub": subject,
            "tv": token_version,
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
