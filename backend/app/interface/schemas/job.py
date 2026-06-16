from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_serializer


def _utc_iso(dt: datetime | None) -> str | None:
    """Serialise a datetime to an ISO-8601 string with explicit UTC suffix.

    The DB stores naive datetimes that are always UTC.  Without the 'Z' suffix
    JavaScript's Date() parses them as local time, making every timestamp
    appear offset by the user's UTC offset (e.g. +5 h in Pakistan).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


class CreateJobRequest(BaseModel):
    usecase_names: list[str] | None = Field(
        default=None,
        description="Specific use cases to run. If omitted, routing engine decides."
    )
    priority: int = Field(default=0, ge=0, le=10)


class JobResponse(BaseModel):
    id: str
    study_instance_uid: str
    usecase_name: str
    status: str
    priority: int = 0
    progress: float = 0.0
    status_message: str = ""
    worker_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_detail: str | None = None
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at", "started_at", "completed_at")
    def serialize_dt(self, dt: datetime | None) -> str | None:
        return _utc_iso(dt)


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
