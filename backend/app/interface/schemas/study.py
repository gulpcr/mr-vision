from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SeriesResponse(BaseModel):
    series_instance_uid: str
    series_number: int | None = None
    series_description: str | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    protocol_name: str | None = None
    num_instances: int = 0
    slice_thickness: float | None = None


class StudyResponse(BaseModel):
    study_instance_uid: str
    patient_id: str | None = None
    patient_name: str | None = None
    study_date: datetime | None = None
    study_description: str | None = None
    accession_number: str | None = None
    referring_physician: str | None = None
    body_part_examined: str | None = None
    modality: str | None = None
    institution_name: str | None = None
    series: list[SeriesResponse] = []
    created_at: datetime
    updated_at: datetime


class StudyListResponse(BaseModel):
    studies: list[StudyResponse]
    total: int
    offset: int
    limit: int


class OrthancStableStudyNotification(BaseModel):
    orthanc_id: str
    study_instance_uid: str = Field(
        ..., pattern=r"^[0-9][0-9.]{0,62}[0-9]$", max_length=64,
    )


class StudyIngestRequest(BaseModel):
    study_instance_uid: str = Field(
        ..., pattern=r"^[0-9][0-9.]{0,62}[0-9]$", max_length=64,
        description="DICOM Study Instance UID (digits and dots, 2-64 chars)",
    )
