from __future__ import annotations

from typing import Any

import structlog

from app.domain.interfaces import ArtifactStore, ResultRepository
from app.domain.models import Result

logger = structlog.get_logger(__name__)


def _flatten_measurements(d: dict, prefix: str = "") -> dict[str, float]:
    result: dict[str, float] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            result[key] = float(v)
        elif isinstance(v, dict):
            result.update(_flatten_measurements(v, key))
    return result


class ResultService:
    """Handles retrieval and serving of AI pipeline results and artifacts."""

    def __init__(
        self,
        result_repo: ResultRepository,
        artifact_store: ArtifactStore,
    ):
        self._result_repo = result_repo
        self._artifact_store = artifact_store

    async def get_result(
        self, study_instance_uid: str, usecase_name: str, version: int | None = None
    ) -> Result | None:
        if version is not None:
            return await self._result_repo.get_by_study_usecase_version(
                study_instance_uid, usecase_name, version
            )
        return await self._result_repo.get_by_study_and_usecase(
            study_instance_uid, usecase_name
        )

    async def list_results_for_study(self, study_instance_uid: str) -> list[Result]:
        return await self._result_repo.list_by_study(study_instance_uid)

    async def list_result_versions(
        self, study_instance_uid: str, usecase_name: str
    ) -> list[Result]:
        return await self._result_repo.list_versions(study_instance_uid, usecase_name)

    async def get_artifact_data(
        self, study_instance_uid: str, usecase_name: str, artifact_path: str
    ) -> bytes:
        storage_path = f"{study_instance_uid}/{usecase_name}/{artifact_path}"
        return await self._artifact_store.get(storage_path)

    async def get_artifact_url(
        self, study_instance_uid: str, usecase_name: str, artifact_path: str
    ) -> str:
        storage_path = f"{study_instance_uid}/{usecase_name}/{artifact_path}"
        return await self._artifact_store.get_presigned_url(storage_path)

    async def get_result_by_id(self, result_id: str) -> Result | None:
        return await self._result_repo.get_by_id(result_id)

    async def compare_results(self, result_id_a: str, result_id_b: str) -> dict:
        result_a = await self._result_repo.get_by_id(result_id_a)
        result_b = await self._result_repo.get_by_id(result_id_b)
        if not result_a:
            raise ValueError(f"Result {result_id_a} not found")
        if not result_b:
            raise ValueError(f"Result {result_id_b} not found")

        flat_a = _flatten_measurements(result_a.measurements)
        flat_b = _flatten_measurements(result_b.measurements)
        all_keys = sorted(set(flat_a) | set(flat_b))

        measurement_deltas: dict = {}
        for key in all_keys:
            val_a = flat_a.get(key)
            val_b = flat_b.get(key)
            if val_a is None or val_b is None:
                continue
            change = val_b - val_a
            change_pct = (change / abs(val_a)) * 100 if val_a != 0 else 0.0
            abs_pct = abs(change_pct)
            severity = "high" if abs_pct >= 25 else "medium" if abs_pct >= 10 else "low"
            measurement_deltas[key] = {
                "a": round(val_a, 3),
                "b": round(val_b, 3),
                "change": round(change, 3),
                "change_pct": round(change_pct, 1),
                "severity": severity,
            }

        flags_a = {f.value if hasattr(f, "value") else f for f in result_a.qa_flags}
        flags_b = {f.value if hasattr(f, "value") else f for f in result_b.qa_flags}
        days_between: int | None = None
        if result_a.created_at and result_b.created_at:
            days_between = abs((result_b.created_at - result_a.created_at).days)

        return {
            "result_a": result_a,
            "result_b": result_b,
            "delta": {
                "measurements": measurement_deltas,
                "qa_flags_new": sorted(flags_b - flags_a),
                "qa_flags_resolved": sorted(flags_a - flags_b),
                "days_between": days_between,
            },
        }

    async def store_artifact(
        self,
        study_instance_uid: str,
        usecase_name: str,
        artifact_path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        storage_path = f"{study_instance_uid}/{usecase_name}/{artifact_path}"
        return await self._artifact_store.put(storage_path, data, content_type)
