"""AuthService.authenticate() must block login for a suspended/offboarded tenant
even with correct credentials — against a real schema (SQLite in-memory), not mocks.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.auth_service import AuthService, TenantAccessError
from app.infrastructure.database.models import Base, TenantRecord, UserRecord


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_user_and_tenant(session, tenant_status: str) -> None:
    session.add(TenantRecord(id="hospital-a", name="Hospital A", slug="hospital-a", status=tenant_status))
    session.add(UserRecord(
        id="user-1",
        username="hospital-a-admin",
        email="admin@hospital-a.example",
        hashed_password=AuthService._hash_password("correct-password"),
        role="admin",
        tenant_id="hospital-a",
        is_active=True,
    ))
    await session.flush()
    await session.commit()


class TestAuthenticateTenantAccess:
    @pytest.mark.asyncio
    async def test_login_succeeds_for_active_tenant(self, db_session):
        await _seed_user_and_tenant(db_session, "active")
        result = await AuthService(db_session).authenticate("hospital-a-admin", "correct-password")
        assert result is not None
        assert result["tenant_id"] == "hospital-a"

    @pytest.mark.asyncio
    async def test_login_blocked_for_suspended_tenant(self, db_session):
        await _seed_user_and_tenant(db_session, "suspended")
        with pytest.raises(TenantAccessError, match="suspended"):
            await AuthService(db_session).authenticate("hospital-a-admin", "correct-password")

    @pytest.mark.asyncio
    async def test_login_blocked_for_offboarded_tenant(self, db_session):
        await _seed_user_and_tenant(db_session, "offboarded")
        with pytest.raises(TenantAccessError, match="offboarded"):
            await AuthService(db_session).authenticate("hospital-a-admin", "correct-password")

    @pytest.mark.asyncio
    async def test_wrong_password_still_returns_none_not_tenant_error(self, db_session):
        """A suspended tenant must not leak its status to someone guessing passwords."""
        await _seed_user_and_tenant(db_session, "suspended")
        result = await AuthService(db_session).authenticate("hospital-a-admin", "wrong-password")
        assert result is None

    @pytest.mark.asyncio
    async def test_login_with_no_matching_tenant_row_is_not_blocked(self, db_session):
        """Missing tenant row (data inconsistency) fails open rather than 500ing login."""
        session = db_session
        session.add(UserRecord(
            id="user-2",
            username="orphan-user",
            email="orphan@example.com",
            hashed_password=AuthService._hash_password("correct-password"),
            role="viewer",
            tenant_id="no-such-tenant",
            is_active=True,
        ))
        await session.commit()

        result = await AuthService(session).authenticate("orphan-user", "correct-password")
        assert result is not None
