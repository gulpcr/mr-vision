"""MFA lockout — after settings.mfa_max_attempts consecutive wrong TOTP/
recovery-code attempts, further attempts are rejected outright (423) until
the lockout window (settings.mfa_lockout_minutes) elapses, closing the
"stolen password + mfa_token can be brute-forced forever" gap. Uses the real
Redis in this container (mfa_lockout.py has no test-friendly seam — it's
the same production code path).
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
from app.infrastructure.ratelimit import mfa_lockout

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


async def _enroll(client, user_id: str, tenant_id: str = "hospital-a") -> tuple[str, list[str]]:
    token = _token(user_id, tenant_id)
    enroll_resp = client.post("/api/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"})
    secret = enroll_resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm_resp = client.post(
        "/api/auth/mfa/confirm", json={"code": code},
        headers={"Authorization": f"Bearer {token}"},
    )
    return secret, confirm_resp.json()["recovery_codes"]


class TestMfaLockout:
    @pytest.mark.asyncio
    async def test_locked_out_after_max_attempts(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        secret, _codes = await _enroll(client, user_id)
        username = f"user-{user_id[:8]}"
        settings = get_settings()

        for _ in range(settings.mfa_max_attempts):
            login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
            mfa_token = login_resp.json()["mfa_token"]
            resp = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})
            assert resp.status_code == 401

        # One more attempt — even with an otherwise-fresh mfa_token — must be
        # rejected outright as locked, not evaluated as right/wrong.
        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]
        correct_code = pyotp.TOTP(secret).now()
        locked_resp = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": correct_code})
        assert locked_resp.status_code == 423
        assert "Retry-After" in locked_resp.headers

    @pytest.mark.asyncio
    async def test_successful_verification_resets_the_counter(self, app_client):
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        secret, _codes = await _enroll(client, user_id)
        username = f"user-{user_id[:8]}"
        settings = get_settings()

        # Fail (max - 1) times, then succeed — should NOT be locked out, and
        # the counter should be back to zero, not just under the threshold.
        for _ in range(settings.mfa_max_attempts - 1):
            login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
            mfa_token = login_resp.json()["mfa_token"]
            client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]
        good_resp = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
        )
        assert good_resp.status_code == 200

        # Fail (max - 1) times again post-reset — must still not be locked,
        # proving the earlier near-miss count didn't carry over.
        for _ in range(settings.mfa_max_attempts - 1):
            login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
            mfa_token = login_resp.json()["mfa_token"]
            resp = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_endpoint_shares_the_same_lockout(self, app_client):
        """verify_code() is the one chokepoint for both login-verify and
        disable — a lockout tripped via /mfa/disable attempts must also
        block /mfa/verify, and vice versa (same user_id-keyed counter).
        """
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        secret, _codes = await _enroll(client, user_id)
        token = _token(user_id)
        settings = get_settings()

        for _ in range(settings.mfa_max_attempts):
            resp = client.post(
                "/api/auth/mfa/disable", json={"code": "000000"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 400

        locked_resp = client.post(
            "/api/auth/mfa/disable", json={"code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert locked_resp.status_code == 423

    @pytest.mark.asyncio
    async def test_lockout_ttl_matches_configured_window(self, monkeypatch):
        """Confirms record_failure() configures Redis's own TTL correctly —
        the actual countdown/expiry is Redis's well-tested mechanism, not
        something worth re-testing via a real (flaky, slow) wall-clock sleep.
        """
        monkeypatch.setenv("MFA_LOCKOUT_MINUTES", "2")
        get_settings.cache_clear()
        user_id = str(uuid.uuid4())

        count = await mfa_lockout.record_failure(user_id)
        assert count == 1

        client = mfa_lockout._get_client()
        ttl = await client.ttl(f"mfa:attempts:{user_id}")
        await client.aclose()
        assert 0 < ttl <= 120

        await mfa_lockout.clear_failures(user_id)

    @pytest.mark.asyncio
    async def test_access_resumes_once_the_counter_is_cleared(self, app_client):
        """Once the failure counter is reset (by real TTL expiry in
        production, or explicitly here) verification works again — this is
        the actual behavior under test; deliberately not coupled to real
        wall-clock timing to avoid a race between the artificial test window
        and the real Redis round-trips each attempt makes (a too-short
        window could expire mid-loop, before the threshold is ever reached).
        """
        client, session_factory = app_client
        user_id = str(uuid.uuid4())
        await _seed_user(session_factory, user_id)
        secret, _codes = await _enroll(client, user_id)
        username = f"user-{user_id[:8]}"
        settings = get_settings()

        for _ in range(settings.mfa_max_attempts):
            login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
            mfa_token = login_resp.json()["mfa_token"]
            client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]
        locked_resp = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"})
        assert locked_resp.status_code == 423

        await mfa_lockout.clear_failures(user_id)

        login_resp = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
        mfa_token = login_resp.json()["mfa_token"]
        good_resp = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
        )
        assert good_resp.status_code == 200


class TestMfaLockoutFailsOpenOnRedisOutage:
    @pytest.mark.asyncio
    async def test_is_locked_fails_open_when_redis_unreachable(self, monkeypatch):
        import redis.asyncio as aioredis

        def _broken_client():
            return aioredis.Redis(host="unreachable-host-for-test", port=1, socket_timeout=1)

        monkeypatch.setattr(mfa_lockout, "_get_client", _broken_client)
        locked, retry_after = await mfa_lockout.is_locked(str(uuid.uuid4()))
        assert locked is False
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_record_failure_does_not_raise_when_redis_unreachable(self, monkeypatch):
        import redis.asyncio as aioredis

        def _broken_client():
            return aioredis.Redis(host="unreachable-host-for-test", port=1, socket_timeout=1)

        monkeypatch.setattr(mfa_lockout, "_get_client", _broken_client)
        count = await mfa_lockout.record_failure(str(uuid.uuid4()))
        assert count == 0
