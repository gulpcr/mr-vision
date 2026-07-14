"""Platform-admin gating, end-to-end through the real FastAPI app (not mocked
services) — the two places `require_platform_admin` protects that this audit
is specifically about: the self-service promote/demote endpoint (item 6) and
the `/admin/reset` all_tenants escape hatch (the most severe bug found in the
earlier tenant-isolation audit).

Uses a StaticPool SQLite in-memory engine (a single shared connection) so data
seeded before the request is still visible to the request's own session, and
real JWTs (minted directly, not via login) so both the platform-admin and
non-platform-admin branches of `require_platform_admin` are actually exercised
— AUTH_MODE=none always sets is_platform_admin=True and can't test the reject
path.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.application.auth_service import derive_tenant_jwt_secret
from app.config import get_settings
from app.infrastructure.database.models import Base, StudyRecord, UserRecord


def _uid() -> str:
    return f"1.2.{uuid.uuid4().int % 10**12}"


def _make_token(user_id: str, username: str, role: str, tenant_id: str, is_platform_admin: bool) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "tenant_id": tenant_id,
        "is_platform_admin": is_platform_admin,
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


async def _seed_user(session_factory, user_id: str, tenant_id: str, is_platform_admin: bool = False) -> None:
    async with session_factory() as session:
        session.add(UserRecord(
            id=user_id,
            username=f"user-{user_id[:8]}",
            email=f"{user_id[:8]}@example.com",
            hashed_password="unused",
            role="admin",
            tenant_id=tenant_id,
            is_active=True,
            is_platform_admin=is_platform_admin,
        ))
        await session.commit()


class TestPlatformAdminPromoteEndpoint:
    @pytest.mark.asyncio
    async def test_rejected_for_non_platform_admin_caller(self, app_client):
        client, session_factory = app_client
        target_id = str(uuid.uuid4())
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, target_id, "hospital-a")
        await _seed_user(session_factory, caller_id, "hospital-a", is_platform_admin=False)

        token = _make_token(caller_id, "caller", "admin", "hospital-a", is_platform_admin=False)
        resp = client.put(
            f"/api/auth/users/{target_id}/platform-admin",
            json={"is_platform_admin": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_succeeds_for_platform_admin_caller(self, app_client):
        client, session_factory = app_client
        target_id = str(uuid.uuid4())
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, target_id, "hospital-a")
        await _seed_user(session_factory, caller_id, "hospital-b", is_platform_admin=True)

        token = _make_token(caller_id, "platform-admin", "admin", "hospital-b", is_platform_admin=True)
        resp = client.put(
            f"/api/auth/users/{target_id}/platform-admin",
            json={"is_platform_admin": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_platform_admin"] is True

        async with session_factory() as session:
            record = (
                await session.execute(select(UserRecord).where(UserRecord.id == target_id))
            ).scalar_one()
            assert record.is_platform_admin is True

    @pytest.mark.asyncio
    async def test_unknown_user_returns_404(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-b", is_platform_admin=True)

        token = _make_token(caller_id, "platform-admin", "admin", "hospital-b", is_platform_admin=True)
        resp = client.put(
            f"/api/auth/users/{uuid.uuid4()}/platform-admin",
            json={"is_platform_admin": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


class TestResetAllDataScoping:
    @pytest.mark.asyncio
    async def test_all_tenants_rejected_for_non_platform_admin(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a", is_platform_admin=False)
        token = _make_token(caller_id, "caller", "admin", "hospital-a", is_platform_admin=False)

        resp = client.post(
            "/api/admin/reset",
            params={"confirm": "true", "all_tenants": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_default_reset_scoped_to_callers_own_tenant(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a", is_platform_admin=False)

        uid_a, uid_b = _uid(), _uid()
        async with session_factory() as session:
            session.add(StudyRecord(study_instance_uid=uid_a, modality="MR", tenant_id="hospital-a"))
            session.add(StudyRecord(study_instance_uid=uid_b, modality="MR", tenant_id="hospital-b"))
            await session.commit()

        token = _make_token(caller_id, "caller", "admin", "hospital-a", is_platform_admin=False)
        resp = client.post(
            "/api/admin/reset",
            params={"confirm": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"]["studies"] == 1

        async with session_factory() as session:
            remaining = (
                await session.execute(select(StudyRecord.study_instance_uid))
            ).scalars().all()
        assert uid_a not in remaining
        assert uid_b in remaining
