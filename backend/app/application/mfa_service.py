"""TOTP-based multi-factor authentication: enrollment, verification, and
one-time recovery codes.

The TOTP secret is encrypted at rest (Fernet, keyed off a purpose-derived
subkey of the platform master secret — see app.config.derive_secret), not
stored plaintext. Recovery codes are stored only as SHA-256 hashes and marked
used on consumption (one-time use), same posture as TenantApiKeyService.
"""
from __future__ import annotations

import base64
import hashlib
import io
import secrets
from datetime import datetime, timezone
from typing import Any

import pyotp
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_ISSUER = "MR Vision"
_RECOVERY_CODE_COUNT = 8


class MfaLockedError(Exception):
    """Raised instead of returning False when the account has exceeded
    settings.mfa_max_attempts — distinct from a plain wrong-code failure so
    callers (the router) can return 423 with a retry-after hint instead of a
    generic 401/400.
    """

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"MFA locked for {retry_after_seconds}s after too many failed attempts")


def _fernet():
    from cryptography.fernet import Fernet

    from app.config import derive_secret

    key_bytes = bytes.fromhex(derive_secret("totp-secret-encryption-v1"))
    return Fernet(base64.urlsafe_b64encode(key_bytes))


class MfaService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def start_enrollment(self, user_id: str, username: str) -> dict[str, Any]:
        """Generates a new TOTP secret and stores it (encrypted) with
        totp_enabled left False — enrollment isn't active until confirm_enrollment
        verifies the user actually scanned it and can produce a valid code.
        """
        from sqlalchemy import update

        from app.infrastructure.database.models import UserRecord

        secret = pyotp.random_base32()
        encrypted = _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")

        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(totp_secret=encrypted, totp_enabled=False)
        )
        await self._session.flush()

        uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=_ISSUER)
        return {
            "secret": secret,
            "otpauth_uri": uri,
            "qr_code_data_uri": self._qr_data_uri(uri),
        }

    async def confirm_enrollment(self, user_id: str, code: str) -> list[str]:
        """Verifies the enrollment code, flips totp_enabled on, and returns a
        fresh batch of recovery codes in PLAINTEXT — the only time they're
        ever visible; only their hashes are persisted.
        """
        from sqlalchemy import select, update

        from app.infrastructure.database.models import MfaRecoveryCodeRecord, UserRecord
        from app.infrastructure.ratelimit import mfa_lockout

        locked, retry_after = await mfa_lockout.is_locked(user_id)
        if locked:
            raise MfaLockedError(retry_after)

        record = (
            await self._session.execute(select(UserRecord).where(UserRecord.id == user_id))
        ).scalar_one_or_none()
        if record is None or not record.totp_secret:
            raise ValueError("No MFA enrollment in progress for this user")

        secret = _fernet().decrypt(record.totp_secret.encode("utf-8")).decode("utf-8")
        if not pyotp.TOTP(secret).verify(code, valid_window=1):
            await mfa_lockout.record_failure(user_id)
            raise ValueError("Invalid verification code")

        await mfa_lockout.clear_failures(user_id)
        await self._session.execute(
            update(UserRecord).where(UserRecord.id == user_id).values(totp_enabled=True)
        )

        codes = [secrets.token_hex(5) for _ in range(_RECOVERY_CODE_COUNT)]
        for plain in codes:
            self._session.add(MfaRecoveryCodeRecord(user_id=user_id, code_hash=self._hash_code(plain)))
        await self._session.flush()
        return codes

    async def disable(self, user_id: str, code: str) -> None:
        """Requires a currently-valid TOTP or recovery code — a password
        alone must not be able to turn MFA off, or a stolen password would
        defeat the whole point of having it.
        """
        from sqlalchemy import delete, update

        from app.infrastructure.database.models import MfaRecoveryCodeRecord, UserRecord

        if not await self.verify_code(user_id, code):
            raise ValueError("Invalid verification code")

        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(totp_enabled=False, totp_secret=None)
        )
        await self._session.execute(
            delete(MfaRecoveryCodeRecord).where(MfaRecoveryCodeRecord.user_id == user_id)
        )
        await self._session.flush()

    async def verify_code(self, user_id: str, code: str) -> bool:
        """Accepts either a live TOTP code or an unused recovery code (the
        recovery code is marked used on success — one-time use).

        Raises MfaLockedError instead of just returning False once the
        account has hit settings.mfa_max_attempts — this is the single
        chokepoint for both the login-time MFA step (AuthService.
        complete_mfa_login) and disable(), so a lockout here covers a
        stolen-password-plus-mfa_token brute force on either path.
        """
        from sqlalchemy import select

        from app.infrastructure.database.models import UserRecord
        from app.infrastructure.ratelimit import mfa_lockout

        locked, retry_after = await mfa_lockout.is_locked(user_id)
        if locked:
            raise MfaLockedError(retry_after)

        record = (
            await self._session.execute(select(UserRecord).where(UserRecord.id == user_id))
        ).scalar_one_or_none()
        if record is None or not record.totp_secret:
            await mfa_lockout.record_failure(user_id)
            return False

        secret = _fernet().decrypt(record.totp_secret.encode("utf-8")).decode("utf-8")
        if pyotp.TOTP(secret).verify(code, valid_window=1):
            await mfa_lockout.clear_failures(user_id)
            return True

        if await self._consume_recovery_code(user_id, code):
            await mfa_lockout.clear_failures(user_id)
            return True

        await mfa_lockout.record_failure(user_id)
        return False

    async def _consume_recovery_code(self, user_id: str, code: str) -> bool:
        from sqlalchemy import update

        from app.infrastructure.database.models import MfaRecoveryCodeRecord

        stmt = (
            update(MfaRecoveryCodeRecord)
            .where(
                MfaRecoveryCodeRecord.user_id == user_id,
                MfaRecoveryCodeRecord.code_hash == self._hash_code(code),
                MfaRecoveryCodeRecord.used_at.is_(None),
            )
            .values(used_at=datetime.now(timezone.utc).replace(tzinfo=None))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @staticmethod
    def _qr_data_uri(otpauth_uri: str) -> str:
        import qrcode

        img = qrcode.make(otpauth_uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
