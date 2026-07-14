"""Operator impersonation — end-to-end through the real FastAPI app with real
JWTs and the real Redis blocklist (this container's Redis, not mocked): a
non-operator must be rejected, a valid operator's minted token must carry the
TARGET's own (never elevated) claims, and /impersonate/stop must actually
revoke that one token going forward.
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
from app.infrastructure.database.models import AuditLogRecord, Base, UserRecord


def _make_token(
    user_id: str, username: str, role: str, tenant_id: str,
    is_platform_admin: bool = False, is_platform_operator: bool = False,
) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "tenant_id": tenant_id,
        "is_platform_admin": is_platform_admin,
        "is_platform_operator": is_platform_operator,
        "jti": str(uuid.uuid4()),
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


async def _seed_user(
    session_factory, user_id: str, tenant_id: str, role: str = "admin",
    is_platform_admin: bool = False, is_platform_operator: bool = False, is_active: bool = True,
) -> None:
    async with session_factory() as session:
        session.add(UserRecord(
            id=user_id,
            username=f"user-{user_id[:8]}",
            email=f"{user_id[:8]}@example.com",
            hashed_password="unused",
            role=role,
            tenant_id=tenant_id,
            is_active=is_active,
            is_platform_admin=is_platform_admin,
            is_platform_operator=is_platform_operator,
        ))
        await session.commit()


class TestStartImpersonation:
    @pytest.mark.asyncio
    async def test_rejected_for_non_operator(self, app_client):
        client, session_factory = app_client
        caller_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a", is_platform_operator=False)
        await _seed_user(session_factory, target_id, "hospital-a", role="viewer")

        token = _make_token(caller_id, "caller", "admin", "hospital-a")
        resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_operator_gets_token_carrying_targets_own_claims(self, app_client):
        client, session_factory = app_client
        operator_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_operator=True)
        await _seed_user(session_factory, target_id, "hospital-a", role="viewer")

        token = _make_token(operator_id, "operator", "admin", "hospital-b", is_platform_operator=True)
        resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == target_id
        assert body["tenant_id"] == "hospital-a"
        assert body["role"] == "viewer"

        claims = jwt.get_unverified_claims(body["access_token"])
        assert claims["sub"] == target_id
        assert claims["tenant_id"] == "hospital-a"
        assert claims["impersonated_by"] == operator_id
        # Never escalated, even though the operator's own token WAS a platform admin.
        assert claims["is_platform_admin"] is False
        assert claims["is_platform_operator"] is False

    @pytest.mark.asyncio
    async def test_platform_admin_is_implicitly_an_operator(self, app_client):
        client, session_factory = app_client
        operator_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_admin=True)
        await _seed_user(session_factory, target_id, "hospital-a")

        token = _make_token(operator_id, "admin-op", "admin", "hospital-b", is_platform_admin=True)
        resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_target_returns_404(self, app_client):
        client, session_factory = app_client
        operator_id = str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_operator=True)

        token = _make_token(operator_id, "operator", "admin", "hospital-b", is_platform_operator=True)
        resp = client.post(
            f"/api/auth/users/{uuid.uuid4()}/impersonate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deactivated_target_is_rejected(self, app_client):
        client, session_factory = app_client
        operator_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_operator=True)
        await _seed_user(session_factory, target_id, "hospital-a", is_active=False)

        token = _make_token(operator_id, "operator", "admin", "hospital-b", is_platform_operator=True)
        resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_start_writes_audit_entry(self, app_client):
        client, session_factory = app_client
        operator_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_operator=True)
        await _seed_user(session_factory, target_id, "hospital-a")

        token = _make_token(operator_id, "operator", "admin", "hospital-b", is_platform_operator=True)
        resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        async with session_factory() as session:
            rows = (await session.execute(
                select(AuditLogRecord).where(AuditLogRecord.action == "impersonation_started")
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == target_id


class TestStopImpersonation:
    @pytest.mark.asyncio
    async def test_rejected_for_a_non_impersonation_token(self, app_client):
        client, session_factory = app_client
        caller_id = str(uuid.uuid4())
        await _seed_user(session_factory, caller_id, "hospital-a")
        token = _make_token(caller_id, "caller", "admin", "hospital-a")

        resp = client.post("/api/auth/impersonate/stop", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_stop_revokes_the_token_going_forward(self, app_client):
        client, session_factory = app_client
        operator_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_operator=True)
        await _seed_user(session_factory, target_id, "hospital-a")

        start_token = _make_token(operator_id, "operator", "admin", "hospital-b", is_platform_operator=True)
        start_resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {start_token}"},
        )
        impersonation_token = start_resp.json()["access_token"]

        # Still valid before stop — hits an authenticated endpoint successfully.
        pre_stop = client.get(
            "/api/auth/users",
            headers={"Authorization": f"Bearer {impersonation_token}"},
        )
        assert pre_stop.status_code in (200, 403)  # 403 only if permission-gated, never 401

        stop_resp = client.post(
            "/api/auth/impersonate/stop",
            headers={"Authorization": f"Bearer {impersonation_token}"},
        )
        assert stop_resp.status_code == 200

        post_stop = client.get(
            "/api/auth/users",
            headers={"Authorization": f"Bearer {impersonation_token}"},
        )
        assert post_stop.status_code == 401

    @pytest.mark.asyncio
    async def test_stop_writes_audit_entry(self, app_client):
        client, session_factory = app_client
        operator_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_user(session_factory, operator_id, "hospital-b", is_platform_operator=True)
        await _seed_user(session_factory, target_id, "hospital-a")

        start_token = _make_token(operator_id, "operator", "admin", "hospital-b", is_platform_operator=True)
        start_resp = client.post(
            f"/api/auth/users/{target_id}/impersonate",
            headers={"Authorization": f"Bearer {start_token}"},
        )
        impersonation_token = start_resp.json()["access_token"]

        client.post(
            "/api/auth/impersonate/stop",
            headers={"Authorization": f"Bearer {impersonation_token}"},
        )

        async with session_factory() as session:
            rows = (await session.execute(
                select(AuditLogRecord).where(AuditLogRecord.action == "impersonation_stopped")
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == target_id
