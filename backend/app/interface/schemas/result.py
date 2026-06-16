from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ArtifactResponse(BaseModel):
    name: str
    artifact_type: str
    storage_path: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0


class ResultResponse(BaseModel):
    id: str
    study_instance_uid: str
    usecase_name: str
    job_id: str
    summary: dict[str, Any] = {}
    measurements: dict[str, Any] = {}
    qa_flags: list[str] = []
    qa_details: dict[str, Any] = {}
    model_version: str
    model_checksum: str
    artifacts: list[ArtifactResponse] = []
    version: int = 1
    is_latest: bool = True
    created_at: datetime


class ResultListResponse(BaseModel):
    results: list[ResultResponse]


class CompareRequest(BaseModel):
    result_ids: list[str]


class MeasurementDelta(BaseModel):
    a: float
    b: float
    change: float
    change_pct: float
    severity: str  # "low" | "medium" | "high"


class DeltaResponse(BaseModel):
    measurements: dict[str, MeasurementDelta]
    qa_flags_new: list[str] = []
    qa_flags_resolved: list[str] = []
    days_between: int | None = None


class CompareResponse(BaseModel):
    usecase_name: str
    result_a: ResultResponse
    result_b: ResultResponse
    delta: DeltaResponse
