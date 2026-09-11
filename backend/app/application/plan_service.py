from __future__ import annotations

from app.domain.interfaces import PlanFeatureRepository


class PlanService:
    """Plan-level feature defaults — see migration 037 for the resolution model."""

    def __init__(self, plan_feature_repo: PlanFeatureRepository):
        self._repo = plan_feature_repo

    async def get_plan_features(self, plan_name: str) -> list[str]:
        return await self._repo.list_by_plan(plan_name)

    async def set_plan_features(self, plan_name: str, feature_keys: list[str]) -> list[str]:
        await self._repo.set_plan_features(plan_name, feature_keys)
        return feature_keys

    async def list_all_plans(self) -> dict[str, list[str]]:
        return await self._repo.list_all_plans()
