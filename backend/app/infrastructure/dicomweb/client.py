from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

DICOM_TAG_MAP = {
    "StudyInstanceUID": "0020000D",
    "SeriesInstanceUID": "0020000E",
    "PatientID": "00100020",
    "PatientName": "00100010",
    "StudyDate": "00080020",
    "StudyDescription": "00081030",
    "AccessionNumber": "00080050",
    "ReferringPhysicianName": "00080090",
    "BodyPartExamined": "00180015",
    "Modality": "00080060",
    "InstitutionName": "00080080",
    "SeriesNumber": "00200011",
    "SeriesDescription": "0008103E",
    "ProtocolName": "00181030",
    "NumberOfFrames": "00280008",
    "SliceThickness": "00180050",
    "PixelSpacing": "00280030",
    "ImageOrientationPatient": "00200037",
    "Rows": "00280010",
    "Columns": "00280011",
    "RepetitionTime": "00180080",
    "EchoTime": "00180081",
    "InversionTime": "00180082",
    "MagneticFieldStrength": "00180087",
    "FlipAngle": "00181314",
    "SequenceName": "00180024",
    "ScanningSequence": "00180020",
    "SequenceVariant": "00180021",
    "MRAcquisitionType": "00180023",
    "NumberOfSeriesRelatedInstances": "00201209",
}


class DICOMwebClient:
    """Lightweight DICOMweb QIDO/WADO-RS client for metadata queries."""

    def __init__(self):
        settings = get_settings()
        self._base_url = settings.dicomweb_url
        self._auth = (settings.orthanc_username, settings.orthanc_password)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    async def qido_studies(self, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        response = await self._client.get(
            "/studies",
            params=params or {},
            headers={"Accept": "application/dicom+json"},
        )
        response.raise_for_status()
        return response.json()

    async def qido_series(
        self, study_uid: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"/studies/{study_uid}/series",
            params=params or {},
            headers={"Accept": "application/dicom+json"},
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()

    @staticmethod
    def extract_tag_value(
        dicom_json: dict[str, Any], tag_keyword: str
    ) -> Any | None:
        tag_id = DICOM_TAG_MAP.get(tag_keyword, tag_keyword)
        tag_data = dicom_json.get(tag_id)
        if not tag_data:
            return None
        value = tag_data.get("Value")
        if not value:
            return None
        first = value[0]
        if isinstance(first, dict):
            return first.get("Alphabetic", str(first))
        return first
