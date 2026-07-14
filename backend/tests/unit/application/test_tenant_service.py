from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.application.tenant_service import TenantService
from app.domain.models import Tenant


@pytest.fixture
def mock_tenant_repo():
    repo = AsyncMock()
    repo.get_by_slug = AsyncMock(return_value=None)
    repo.get_by_id = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    repo.update = AsyncMock()
    repo.list_all = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def service(mock_tenant_repo):
    return TenantService(tenant_repo=mock_tenant_repo)


class TestCreateTenant:
    async def test_creates_active_tenant_with_defaults(self, service, mock_tenant_repo):
        mock_tenant_repo.save.side_effect = lambda t: t

        tenant = await service.create_tenant(name="Hospital A", slug="hospital-a")

        assert tenant.id == "hospital-a"
        assert tenant.slug == "hospital-a"
        assert tenant.status == "active"
        assert tenant.is_active is True
        assert tenant.plan == "starter"
        assert tenant.features == []
        mock_tenant_repo.save.assert_awaited_once()

    async def test_rejects_duplicate_slug(self, service, mock_tenant_repo):
        mock_tenant_repo.get_by_slug.return_value = Tenant(id="hospital-a", slug="hospital-a")

        with pytest.raises(ValueError, match="already exists"):
            await service.create_tenant(name="Hospital A Again", slug="hospital-a")

        mock_tenant_repo.save.assert_not_called()


class TestUpdateFeatures:
    async def test_updates_features_on_existing_tenant(self, service, mock_tenant_repo):
        existing = Tenant(id="hospital-a", slug="hospital-a", features=[])
        mock_tenant_repo.get_by_id.return_value = existing
        mock_tenant_repo.update.side_effect = lambda t: t

        updated = await service.update_features("hospital-a", ["brain_mri", "cds"])

        assert updated.features == ["brain_mri", "cds"]
        mock_tenant_repo.update.assert_awaited_once()

    async def test_raises_on_missing_tenant(self, service, mock_tenant_repo):
        mock_tenant_repo.get_by_id.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await service.update_features("no-such-tenant", ["brain_mri"])


class TestSetStatus:
    async def test_suspend_flips_is_active_false(self, service, mock_tenant_repo):
        existing = Tenant(id="hospital-a", slug="hospital-a", status="active", is_active=True)
        mock_tenant_repo.get_by_id.return_value = existing
        mock_tenant_repo.update.side_effect = lambda t: t

        updated = await service.set_status("hospital-a", "suspended")

        assert updated.status == "suspended"
        assert updated.is_active is False

    async def test_reactivate_flips_is_active_true(self, service, mock_tenant_repo):
        existing = Tenant(id="hospital-a", slug="hospital-a", status="suspended", is_active=False)
        mock_tenant_repo.get_by_id.return_value = existing
        mock_tenant_repo.update.side_effect = lambda t: t

        updated = await service.set_status("hospital-a", "active")

        assert updated.status == "active"
        assert updated.is_active is True

    async def test_rejects_invalid_status(self, service, mock_tenant_repo):
        with pytest.raises(ValueError, match="Invalid status"):
            await service.set_status("hospital-a", "deleted")

        mock_tenant_repo.get_by_id.assert_not_called()
