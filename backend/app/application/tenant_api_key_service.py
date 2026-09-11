from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

import structlog

from app.domain.interfaces import TenantApiKeyRepository
from app.domain.models import TenantApiKey

logger = structlog.get_logger(__name__)

KEY_PREFIX = "mrv_"


class TenantApiKeyService:
    """Scoped, revocable API keys handed to a tenant's external systems (e.g.
    their DICOM sender) — distinct from the single global admin api_key in
    Settings, which grants unscoped platform access.
    """

    def __init__(self, key_repo: TenantApiKeyRepository):
        self._key_repo = key_repo

    async def create_key(
        self,
        tenant_id: str,
        name: str,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> tuple[TenantApiKey, str]:
        plain, key_hash, prefix = self._generate_key()
        key = TenantApiKey(
            tenant_id=tenant_id,
            name=name,
            key_hash=key_hash,
            prefix=prefix,
            scopes=scopes,
            expires_at=expires_at,
            is_active=True,
        )
        saved = await self._key_repo.save(key)
        logger.info("tenant_api_key_created", tenant_id=tenant_id, key_id=saved.id, scopes=scopes)
        return saved, plain

    async def list_keys(self, tenant_id: str) -> list[TenantApiKey]:
        return await self._key_repo.list_by_tenant(tenant_id)

    async def revoke_key(self, tenant_id: str, key_id: str) -> TenantApiKey:
        key = await self._key_repo.get_by_id(key_id)
        if key is None or key.tenant_id != tenant_id:
            raise ValueError(f"API key '{key_id}' not found for this tenant")
        key.is_active = False
        key.revoked_at = datetime.now(timezone.utc)
        updated = await self._key_repo.update(key)
        logger.info("tenant_api_key_revoked", tenant_id=tenant_id, key_id=key_id)
        return updated

    async def validate_key(self, plaintext: str) -> TenantApiKey:
        """Raises ValueError on any invalid/inactive/expired/revoked key."""
        key_hash = self.hash_key(plaintext)
        key = await self._key_repo.get_by_hash(key_hash)
        if key is None or not key.is_active:
            raise ValueError("Invalid or revoked API key")
        if key.is_expired():
            raise ValueError("API key has expired")

        key.last_used_at = datetime.now(timezone.utc)
        await self._key_repo.update(key)
        return key

    @staticmethod
    def hash_key(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @staticmethod
    def _generate_key() -> tuple[str, str, str]:
        random_part = secrets.token_hex(32)
        plain = f"{KEY_PREFIX}{random_part}"
        key_hash = TenantApiKeyService.hash_key(plain)
        prefix = plain[:12]
        return plain, key_hash, prefix
