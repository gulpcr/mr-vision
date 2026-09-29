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


class SliceWindow(BaseModel):
    name: str
    level: float
    width: float


class SlicePanel(BaseModel):
    """One sequence panel of an MRI montage tile, resolved to its own series' image."""

    sequence: str
    rect: list[float]  # [x0, y0, x1, y1] as fractions of the tile image
    is_reference: bool = False
    clickable: bool = False
    reason: str | None = None  # why the panel is not linked
    series_instance_uid: str | None = None
    sop_instance_uid: str | None = None
    instance_number: int | None = None
    stack_position: int | None = None
    n_images: int | None = None
    slice_offset: float | None = None  # fraction of a slice between the panel and the image


class SliceLinkTile(BaseModel):
    artifact_name: str
    z: int
    window: SliceWindow | None = None
    # CT: the tile's image. MRI: the montage's primary (reference) panel, if linked.
    sop_instance_uid: str | None = None
    series_instance_uid: str | None = None  # MRI only (CT uses the response's series)
    instance_number: int | None = None
    stack_position: int | None = None
    reported: bool = False  # got the detailed (Pass-2) read; appears in the report
    overview: bool = False  # evenly-spread preview tile, stored flagged or not
    screen_flagged: bool = False  # flagged by the Pass-1 screening scan
    finding: str | None = None
    plane: str | None = None  # MRI only
    panels: list[SlicePanel] = []  # MRI only


class FlaggedImage(BaseModel):
    z: int
    series_instance_uid: str | None = None  # MRI only (each panel's own series)
    sop_instance_uid: str
    instance_number: int | None = None
    stack_position: int | None = None
    finding: str | None = None
    reported: bool = False  # False → screening-only flag (not in the detailed read)


class FlaggedSlicesResponse(BaseModel):
    """CT/MRI report slice tiles resolved to the exact DICOM images the viewer shows."""

    supported: bool
    resolved: bool
    reason: str | None = None
    series_instance_uid: str | None = None
    series_description: str | None = None
    spacing_irregular: bool = False
    tiles: list[SliceLinkTile] = []
    flagged_images: list[FlaggedImage] = []
