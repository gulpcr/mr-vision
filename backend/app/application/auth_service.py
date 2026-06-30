from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.config import get_settings
from app.domain.enums import AuditAction
from app.domain.models import AuditEntry, User

logger = structlog.get_logger(__name__)


class AuthService:
    """Handles user authentication and token management."""

    def __init__(self, session):
        self._session = session

    async def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Verify credentials and return JWT tokens."""
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

        access_token = self._create_access_token(
            subject=user_record.id,
            username=user_record.username,
            role=user_record.role,
            tenant_id=user_record.tenant_id,
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user_id": user_record.id,
            "username": user_record.username,
            "role": user_record.role,
            "tenant_id": user_record.tenant_id,
        }

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        full_name: str = "",
        role: str = "viewer",
        tenant_id: str = "default",
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
        )
        self._session.add(record)
        await self._session.flush()

        return User(
            id=user_id,
            username=username,
            email=email,
            hashed_password=hashed,
            full_name=full_name,
            role=role,
            tenant_id=tenant_id,
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

    def decode_token(self, token: str) -> dict[str, Any] | None:
        """Decode and validate a JWT token."""
        try:
            from jose import jwt, JWTError
            settings = get_settings()
            payload = jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
            )
            return payload
        except Exception:
            return None

    def _create_access_token(
        self,
        subject: str,
        username: str,
        role: str,
        tenant_id: str,
    ) -> str:
        from jose import jwt
        settings = get_settings()
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.jwt_access_token_expire_minutes
        )
        payload = {
            "sub": subject,
            "username": username,
            "role": role,
            "tenant_id": tenant_id,
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

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
