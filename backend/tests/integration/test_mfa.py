"""TOTP-based MFA — end-to-end through the real FastAPI app: enrollment,
the two-step login flow, recovery-code one-time use, and the specific gap
this feature closes (an mfa_pending token must never work as a normal Bearer
token before the code is verified).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.application.auth_service import AuthService, derive_tenant_jwt_secret
from app.config import get_settings
from app.infrastructure.database.models import Base, UserRecord

_PASSWORD = "correct-horse-battery-staple"


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


async def _seed_user(session_factory, user_id: str, tenant_id: str = "hospital-a") -> None:
    async with session_factory() as session:
        session.add(UserRecord(
            id=user_id,
            username=f"user-{user_id[:8]}",
            email=f"{user_id[:8]}@example.com",
            hashed_password=AuthService._hash_password(_PASSWORD),
            role="admin",
            tenant_id=tenant_id,
            is_active=True,
        ))
        await session.commit()


def _token(user_id: str, tenant_id: str = "hospital-a") -> str:
    settings = get_settings()
    payload = {
        "sub": user_id, "username": f"user-{user_id[:8]}", "role": "admin",
        "tenant_id": tenant_id, "is_platform_admin": False, "is_platform_operator": False,
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, derive_tenant_jwt_secret(tenant_id), algorithm=settings.jwt_algorithm)


async def _enroll(client, session_factory, user_id: str, tenant_id: str = "hospital-a") -> tuple[str, list[str]]:
    """Enrolls MFA for user_id and returns (totp_secret, recovery_codes)."""
    token = _token(user_id, tenant_id)
    enroll_resp = client.post("/api/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"})
    secret = enroll_resp.json()["secret"]

    code = pyotp.TOTP(secret).now()
    confirm_resp = client.post(
        "/api/auth/mfa/confirm", json={"code": code},
        headers={"Authorization": f"Bearer {token}"},
    )
    return secret, confirm_resp.json()["recovery_codes"]


class TestEnrollment:
    @pytest.mark.asyncio
    async def test_enroll_returns_secret_and_qr(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        token = _token(user_id)

        resp = client.post("/api/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["secret"]) >= 16
        assert body["qr_code_data_uri"].startswith("data:image/png;base64,")
        assert body["otpauth_uri"].startswith("otpauth://totp/")

    @pytest.mark.asyncio
    async def test_confirm_with_wrong_code_rejected(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        token = _token(user_id)

        client.post("/api/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"})
        resp = client.post(
            "/api/auth/mfa/confirm", json={"code": "000000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_confirm_with_correct_code_returns_recovery_codes(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)

        secret, codes = await _enroll(client, session_factory, user_id)
        assert len(codes) == 8
        assert len({*codes}) == 8  # all distinct


class TestTwoStepLogin:
    @pytest.mark.asyncio
    async def test_login_without_mfa_returns_real_token_directly(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        username = f"user-{user_id[:8]}"

        resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mfa_required"] is False
        assert body["access_token"]

    @pytest.mark.asyncio
    async def test_login_with_mfa_enabled_returns_mfa_token_not_access_token(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        await _enroll(client, session_factory, user_id)
        username = f"user-{user_id[:8]}"

        resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mfa_required"] is True
        assert body["mfa_token"]
        assert body["access_token"] is None

    @pytest.mark.asyncio
    async def test_verify_with_correct_totp_issues_real_token(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        secret, _codes = await _enroll(client, session_factory, user_id)
        username = f"user-{user_id[:8]}"

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]

        code = pyotp.TOTP(secret).now()
        verify_resp = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code})
        assert verify_resp.status_code == 200
        body = verify_resp.json()
        assert body["mfa_required"] is False
        assert body["access_token"]
        assert body["user_id"] == user_id

    @pytest.mark.asyncio
    async def test_verify_with_wrong_code_rejected(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        await _enroll(client, session_factory, user_id)
        username = f"user-{user_id[:8]}"

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]

        resp = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_verify_with_recovery_code_works_once_only(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        _secret, codes = await _enroll(client, session_factory, user_id)
        username = f"user-{user_id[:8]}"

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token_1 = login_resp.json()["mfa_token"]
        first = client.post(
            "/api/auth/mfa/verify", json={"mfa_token": mfa_token_1, "code": codes[0]},
        )
        assert first.status_code == 200

        login_resp_2 = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token_2 = login_resp_2.json()["mfa_token"]
        second = client.post(
            "/api/auth/mfa/verify", json={"mfa_token": mfa_token_2, "code": codes[0]},
        )
        assert second.status_code == 401

    @pytest.mark.asyncio
    async def test_mfa_pending_token_cannot_be_used_as_a_bearer_token(self, app_client):
        """The core gap this feature must close: a leaked mfa_token must not
        authenticate real requests before the code is verified.
        """
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        await _enroll(client, session_factory, user_id)
        username = f"user-{user_id[:8]}"

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]

        resp = client.get("/api/auth/users", headers={"Authorization": f"Bearer {mfa_token}"})
        assert resp.status_code == 401


class TestDisable:
    @pytest.mark.asyncio
    async def test_disable_requires_valid_code(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        await _enroll(client, session_factory, user_id)
        token = _token(user_id)

        resp = client.post(
            "/api/auth/mfa/disable", json={"code": "000000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_disable_then_login_no_longer_requires_mfa(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        secret, _codes = await _enroll(client, session_factory, user_id)
        username = f"user-{user_id[:8]}"
        token = _token(user_id)

        code = pyotp.TOTP(secret).now()
        disable_resp = client.post(
            "/api/auth/mfa/disable", json={"code": code},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert disable_resp.status_code == 200

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        assert login_resp.json()["mfa_required"] is False
