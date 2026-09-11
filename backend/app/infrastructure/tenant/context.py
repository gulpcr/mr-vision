from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

_current_tenant: ContextVar["TenantContext | None"] = ContextVar("current_tenant", default=None)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    slug: str
    plan: str
    features: list[str] = field(default_factory=list)

    def has_feature(self, feature_key: str) -> bool:
        return feature_key in self.features


class TenantContextService:
    """ContextVar-backed accessor for the tenant bound to the current request/task scope."""

    @staticmethod
    def get_context() -> TenantContext:
        ctx = _current_tenant.get()
        if ctx is None:
            raise LookupError("No tenant context is bound to the current execution scope")
        return ctx

    @staticmethod
    def get_context_or_null() -> TenantContext | None:
        return _current_tenant.get()

    @staticmethod
    def set_context(ctx: TenantContext) -> Token:
        return _current_tenant.set(ctx)

    @staticmethod
    def reset_context(token: Token) -> None:
        _current_tenant.reset(token)

    @staticmethod
    def has_feature(feature_key: str) -> bool:
        ctx = _current_tenant.get()
        return ctx is not None and ctx.has_feature(feature_key)
