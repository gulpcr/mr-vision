from __future__ import annotations

from typing import Any

import structlog

from app.domain.interfaces import ArtifactStore, ResultRepository
from app.domain.models import Result
from app.domain.storage_keys import artifact_key, legacy_artifact_key

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



class ArtifactIntegrityError(Exception):
    """A stored artifact no longer matches the SHA-256 recorded when it was written."""

class ResultService:
    """Handles retrieval and serving of AI pipeline results and artifacts."""

    def __init__(
        self,
        result_repo: ResultRepository,
        artifact_store: ArtifactStore,
        tenant_id: str = "default",
    ):
        self._result_repo = result_repo
        self._artifact_store = artifact_store
        # The caller's tenant: artifacts are stored under a {tenant_id}/ key prefix.
        self._tenant_id = tenant_id or "default"

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

    async def _expected_sha256(
        self, study_instance_uid: str, usecase_name: str, artifact_path: str, key: str
    ) -> str | None:
        for r in await self._result_repo.list_by_study(study_instance_uid):
            if r.usecase_name != usecase_name:
                continue
            for a in getattr(r, "artifacts", None) or []:
                if a.sha256 and (a.storage_path == key or a.name == artifact_path):
                    return a.sha256
        return None

    async def _ensure_artifact_visible(self, study_instance_uid: str, usecase_name: str) -> None:
        """The object store is shared by every tenant and has no notion of tenancy, so
        an artifact is only served when the caller's (tenant-scoped) result repository
        can see a result for that study + use case. Without this, any user could read
        another tenant's artifacts by guessing a StudyInstanceUID."""
        results = await self._result_repo.list_by_study(study_instance_uid)
        if not any(r.usecase_name == usecase_name for r in results):
            raise ValueError(f"No result for {study_instance_uid}/{usecase_name}")

    async def _resolve_artifact_key(
        self, study_instance_uid: str, usecase_name: str, artifact_path: str
    ) -> str:
        """Tenant-prefixed key if that object exists, else the pre-tenancy legacy key
        (objects not yet moved by scripts/migrate_artifact_keys.py)."""
        key = artifact_key(self._tenant_id, study_instance_uid, usecase_name, artifact_path)
        if await self._artifact_store.exists(key):
            return key
        return legacy_artifact_key(study_instance_uid, usecase_name, artifact_path)

    async def get_artifact_data(
        self, study_instance_uid: str, usecase_name: str, artifact_path: str
    ) -> bytes:
        await self._ensure_artifact_visible(study_instance_uid, usecase_name)
        key = await self._resolve_artifact_key(study_instance_uid, usecase_name, artifact_path)
        data = await self._artifact_store.get(key)
        expected = await self._expected_sha256(study_instance_uid, usecase_name, artifact_path, key)
        if expected is not None:
            import hashlib

            if hashlib.sha256(data).hexdigest() != expected:
                raise ArtifactIntegrityError(key)
        return data

    async def get_artifact_url(
        self, study_instance_uid: str, usecase_name: str, artifact_path: str
    ) -> str:
        await self._ensure_artifact_visible(study_instance_uid, usecase_name)
        key = await self._resolve_artifact_key(study_instance_uid, usecase_name, artifact_path)
        return await self._artifact_store.get_presigned_url(key)

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
        storage_path = artifact_key(self._tenant_id, study_instance_uid, usecase_name, artifact_path)
        return await self._artifact_store.put(storage_path, data, content_type)
