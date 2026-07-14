from __future__ import annotations

import pytest

from app.infrastructure.tenant.context import TenantContext, TenantContextService


@pytest.fixture(autouse=True)
def _reset_context():
    """Every test starts with no tenant bound, regardless of test order."""
    token = TenantContextService.set_context(None)
    TenantContextService.reset_context(token)
    yield
    token = TenantContextService.set_context(None)
    TenantContextService.reset_context(token)


class TestTenantContextService:
    def test_get_context_or_null_when_unbound(self):
        assert TenantContextService.get_context_or_null() is None

    def test_get_context_raises_when_unbound(self):
        with pytest.raises(LookupError):
            TenantContextService.get_context()

    def test_set_and_get_context(self):
        ctx = TenantContext(tenant_id="hospital-a", slug="hospital-a", plan="starter")
        token = TenantContextService.set_context(ctx)
        try:
            assert TenantContextService.get_context_or_null() is ctx
            assert TenantContextService.get_context().tenant_id == "hospital-a"
        finally:
            TenantContextService.reset_context(token)
        assert TenantContextService.get_context_or_null() is None

    def test_reset_restores_prior_context(self):
        outer = TenantContext(tenant_id="hospital-a", slug="hospital-a", plan="starter")
        inner = TenantContext(tenant_id="hospital-b", slug="hospital-b", plan="starter")

        outer_token = TenantContextService.set_context(outer)
        inner_token = TenantContextService.set_context(inner)
        assert TenantContextService.get_context().tenant_id == "hospital-b"

        TenantContextService.reset_context(inner_token)
        assert TenantContextService.get_context().tenant_id == "hospital-a"

        TenantContextService.reset_context(outer_token)
        assert TenantContextService.get_context_or_null() is None


class TestHasFeature:
    def test_has_feature_false_when_unbound(self):
        assert TenantContextService.has_feature("brain_mri") is False

    def test_has_feature_true_when_present(self):
        ctx = TenantContext(
            tenant_id="hospital-a", slug="hospital-a", plan="starter",
            features=["brain_mri", "cds"],
        )
        token = TenantContextService.set_context(ctx)
        try:
            assert TenantContextService.has_feature("brain_mri") is True
            assert TenantContextService.has_feature("mammography") is False
        finally:
            TenantContextService.reset_context(token)

    def test_context_has_feature_method(self):
        ctx = TenantContext(
            tenant_id="hospital-a", slug="hospital-a", plan="starter", features=["cds"],
        )
        assert ctx.has_feature("cds") is True
        assert ctx.has_feature("longitudinal") is False
