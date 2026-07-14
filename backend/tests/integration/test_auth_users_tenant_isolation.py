"""GET /auth/users tenant isolation — previously called service.list_users()
with NO filter at all, returning EVERY tenant's users to anyone holding the
per-tenant "user.manage" permission (auto-granted to role=="admin"). Default
behavior must now be scoped to the caller's own tenant, and viewing another
tenant's users must require platform-admin.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.application.auth_service import derive_tenant_jwt_secret
from app.config import get_settings
from app.infrastructure.database.models import Base, UserRecord


def _make_token(user_id: str, tenant_id: str, is_platform_admin: bool = False) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id, "username": f"user-{user_id[:8]}", "role": "admin",
        "tenant_id": tenant_id, "is_platform_admin": is_platform_admin,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, derive_tenant_jwt_secret(tenant_id), algorithm=settings.jwt_algorithm)


@pytest_asyncio.fixture
async def app_client(monkeypatch):
    from app.interface.api.dependencies import get_session
    from app.main import create_app

    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_session():
        async with session_factory() as session:
            yield session
            await session.commit()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session

    client = TestClient(app)
    yield client, session_factory
    await engine.dispose()
    get_settings.cache_clear()


async def _seed_user(session_factory, user_id: str, tenant_id: str) -> None:
    async with session_factory() as session:
        session.add(UserRecord(
            id=user_id, username=f"user-{user_id[:8]}", email=f"{user_id[:8]}@example.com",
            hashed_password="unused", role="admin", tenant_id=tenant_id, is_active=True,
        ))
        await session.commit()


class TestAuthUsersTenantIsolation:
    @pytest.mark.asyncio
    async def test_default_call_only_returns_callers_own_tenant(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a")
        await _seed_user(session_factory, other_id, "hospital-b")

        token = _make_token(caller_id, "hospital-a")
        resp = client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        returned_ids = {u["id"] for u in resp.json()}
        assert caller_id in returned_ids
        assert other_id not in returned_ids

    @pytest.mark.asyncio
    async def test_explicit_other_tenant_rejected_for_non_platform_admin(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a")

        token = _make_token(caller_id, "hospital-a", is_platform_admin=False)
        resp = client.get(
            "/api/auth/users", params={"tenant_id": "hospital-b"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_explicit_other_tenant_allowed_for_platform_admin(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a")
        await _seed_user(session_factory, other_id, "hospital-b")

        token = _make_token(caller_id, "hospital-a", is_platform_admin=True)
        resp = client.get(
            "/api/auth/users", params={"tenant_id": "hospital-b"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        returned_ids = {u["id"] for u in resp.json()}
        assert returned_ids == {other_id}
