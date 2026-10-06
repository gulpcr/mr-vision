"""Artifacts are served only if they still match the SHA-256 recorded at write time."""
from __future__ import annotations

import hashlib

import pytest

from app.application.result_service import ArtifactIntegrityError, ResultService
from app.domain.models import Result, ResultArtifact


class FakeRepo:
    def __init__(self, result):
        self._result = result

    async def list_by_study(self, study_uid):
        return [self._result]


class FakeStore:
    def __init__(self, data):
        self.data = data

    async def exists(self, key):
        return True

    async def get(self, key):
        return self.data


def _service(stored: bytes, recorded: bytes | None) -> ResultService:
    art = ResultArtifact(
        name="tile.png", artifact_type="x_slice_png", storage_path="t/1.2.3/abdomen_ct/tile.png",
        sha256=hashlib.sha256(recorded).hexdigest() if recorded is not None else None,
    )
    result = Result(study_instance_uid="1.2.3", usecase_name="abdomen_ct", artifacts=[art])
    svc = ResultService(result_repo=FakeRepo(result), artifact_store=FakeStore(stored))
    svc._tenant_id = "t"
    return svc


async def test_matching_bytes_are_served():
    assert await _service(b"pixels", b"pixels").get_artifact_data("1.2.3", "abdomen_ct", "tile.png") == b"pixels"


async def test_altered_bytes_are_refused():
    with pytest.raises(ArtifactIntegrityError):
        await _service(b"tampered", b"pixels").get_artifact_data("1.2.3", "abdomen_ct", "tile.png")


async def test_artifacts_from_before_hashing_are_still_served():
    assert await _service(b"old", None).get_artifact_data("1.2.3", "abdomen_ct", "tile.png") == b"old"
