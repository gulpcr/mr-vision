from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pydicom
import SimpleITK as sitk
import structlog

from app.config import get_settings
from app.domain.interfaces import PACSClient

logger = structlog.get_logger(__name__)


class OrthancPACSClient(PACSClient):
    """Orthanc REST + DICOMweb client for study/series retrieval and DICOM-to-NIfTI conversion."""

    def __init__(self):
        settings = get_settings()
        self._base_url = settings.orthanc_url
        self._dicomweb_url = settings.dicomweb_url
        self._auth = (settings.orthanc_username, settings.orthanc_password)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def get_study(self, study_instance_uid: str) -> dict[str, Any]:
        """Fetch study metadata. Tries DICOMweb QIDO first, falls back to Orthanc REST."""
        # Try DICOMweb QIDO-RS first
        try:
            response = await self._client.get(
                "/dicom-web/studies",
                params={"StudyInstanceUID": study_instance_uid, "includefield": "all"},
                headers={"Accept": "application/dicom+json"},
            )
            response.raise_for_status()
            results = response.json()
            if results:
                return results[0]
        except Exception as exc:
            logger.warning("dicomweb_qido_failed", error=str(exc))

        # Fallback: Orthanc REST API /tools/find
        logger.info("falling_back_to_orthanc_rest", study_uid=study_instance_uid)
        response = await self._client.post(
            "/tools/find",
            json={
                "Level": "Study",
                "Query": {"StudyInstanceUID": study_instance_uid},
                "Expand": True,
            },
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            raise ValueError(
                f"Study {study_instance_uid} not found in Orthanc. "
                f"Use GET /api/orthanc/studies to list all available studies."
            )

        # Convert Orthanc REST format to DICOMweb-like format for downstream compatibility
        orthanc_study = results[0]
        tags = orthanc_study.get("MainDicomTags", {})
        patient = orthanc_study.get("PatientMainDicomTags", {})
        return self._orthanc_to_dicomweb_study(tags, patient)

    def _orthanc_to_dicomweb_study(
        self, tags: dict[str, str], patient: dict[str, str]
    ) -> dict[str, Any]:
        """Convert Orthanc REST study tags to DICOMweb JSON format."""
        mapping = {
            "StudyInstanceUID": "0020000D",
            "StudyDate": "00080020",
            "StudyDescription": "00081030",
            "AccessionNumber": "00080050",
            "ReferringPhysicianName": "00080090",
            "InstitutionName": "00080080",
            "PatientID": "00100020",
            "PatientName": "00100010",
        }
        result: dict[str, Any] = {}
        all_tags = {**tags, **patient}
        for keyword, tag_id in mapping.items():
            value = all_tags.get(keyword)
            if value is not None:
                if keyword in ("PatientName", "ReferringPhysicianName"):
                    result[tag_id] = {"vr": "PN", "Value": [{"Alphabetic": value}]}
                else:
                    result[tag_id] = {"vr": "LO", "Value": [value]}
        return result

    async def get_series_list(self, study_instance_uid: str) -> list[dict[str, Any]]:
        """Fetch series list. Tries DICOMweb first, falls back to Orthanc REST."""
        # Try DICOMweb first
        try:
            response = await self._client.get(
                f"/dicom-web/studies/{study_instance_uid}/series",
                headers={"Accept": "application/dicom+json"},
            )
            response.raise_for_status()
            results = response.json()
            if results:
                return results
        except Exception as exc:
            logger.warning("dicomweb_series_failed", error=str(exc))

        # Fallback: Orthanc REST
        logger.info("falling_back_to_orthanc_rest_series", study_uid=study_instance_uid)
        orthanc_study_id = await self.get_orthanc_study_id(study_instance_uid)
        response = await self._client.get(f"/studies/{orthanc_study_id}")
        response.raise_for_status()
        study_info = response.json()

        series_list = []
        for series_id in study_info.get("Series", []):
            resp = await self._client.get(f"/series/{series_id}")
            resp.raise_for_status()
            s_info = resp.json()
            s_tags = s_info.get("MainDicomTags", {})
            num_instances = len(s_info.get("Instances", []))
            series_list.append(
                self._orthanc_to_dicomweb_series(s_tags, study_instance_uid, num_instances)
            )
        return series_list

    def _orthanc_to_dicomweb_series(
        self, tags: dict[str, str], study_uid: str, num_instances: int = 0
    ) -> dict[str, Any]:
        """Convert Orthanc REST series tags to DICOMweb JSON format."""
        mapping = {
            "SeriesInstanceUID": "0020000E",
            "Modality": "00080060",
            "SeriesDescription": "0008103E",
            "SeriesNumber": "00200011",
            "BodyPartExamined": "00180015",
            "ProtocolName": "00181030",
            "SliceThickness": "00180050",
        }
        result: dict[str, Any] = {
            "0020000D": {"vr": "UI", "Value": [study_uid]},
        }
        for keyword, tag_id in mapping.items():
            value = tags.get(keyword)
            if value is not None:
                result[tag_id] = {"vr": "LO", "Value": [value]}
        # NumberOfSeriesRelatedInstances (used by study_service for num_instances)
        if num_instances > 0:
            result["00201209"] = {"vr": "IS", "Value": [num_instances]}
        return result

    async def list_all_studies(self) -> list[dict[str, Any]]:
        """List all studies in Orthanc via REST API."""
        response = await self._client.get("/studies")
        response.raise_for_status()
        study_ids = response.json()

        studies = []
        for study_id in study_ids:
            resp = await self._client.get(f"/studies/{study_id}")
            resp.raise_for_status()
            info = resp.json()
            tags = info.get("MainDicomTags", {})
            patient = info.get("PatientMainDicomTags", {})
            studies.append({
                "orthanc_id": study_id,
                "study_instance_uid": tags.get("StudyInstanceUID", ""),
                "patient_id": patient.get("PatientID", ""),
                "patient_name": patient.get("PatientName", ""),
                "study_date": tags.get("StudyDate", ""),
                "study_description": tags.get("StudyDescription", ""),
                "modality": tags.get("ModalitiesInStudy", ""),
                "series_count": len(info.get("Series", [])),
            })
        return studies

    async def get_orthanc_study_id(self, study_instance_uid: str) -> str:
        response = await self._client.post(
            "/tools/lookup",
            content=study_instance_uid,
        )
        response.raise_for_status()
        results = response.json()
        for item in results:
            if item.get("Type") == "Study":
                return item["ID"]
        raise ValueError(f"No Orthanc study found for UID {study_instance_uid}")

    async def get_orthanc_series_id(self, series_instance_uid: str) -> str:
        response = await self._client.post(
            "/tools/lookup",
            content=series_instance_uid,
        )
        response.raise_for_status()
        results = response.json()
        for item in results:
            if item.get("Type") == "Series":
                return item["ID"]
        raise ValueError(f"No Orthanc series found for UID {series_instance_uid}")

    async def download_series_dicoms(
        self, study_instance_uid: str, series_instance_uid: str, output_dir: str
    ) -> list[str]:
        os.makedirs(output_dir, exist_ok=True)
        orthanc_series_id = await self.get_orthanc_series_id(series_instance_uid)
        response = await self._client.get(f"/series/{orthanc_series_id}")
        response.raise_for_status()
        series_info = response.json()
        instance_ids = series_info.get("Instances", [])
        if not instance_ids:
            raise ValueError(
                f"No instances found for series {series_instance_uid}"
            )
        logger.info(
            "downloading_series_dicoms",
            series_uid=series_instance_uid,
            instance_count=len(instance_ids),
        )
        dicom_paths = []
        for idx, instance_id in enumerate(instance_ids):
            resp = await self._client.get(
                f"/instances/{instance_id}/file",
                headers={"Accept": "application/dicom"},
            )
            resp.raise_for_status()
            file_path = os.path.join(output_dir, f"{idx:06d}.dcm")
            with open(file_path, "wb") as f:
                f.write(resp.content)
            dicom_paths.append(file_path)
        logger.info(
            "downloaded_series_dicoms",
            series_uid=series_instance_uid,
            file_count=len(dicom_paths),
        )
        return sorted(dicom_paths)

    async def download_series_as_nifti(
        self, study_instance_uid: str, series_instance_uid: str, output_path: str
    ) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            dicom_dir = os.path.join(tmpdir, "dicoms")
            dicom_paths = await self.download_series_dicoms(
                study_instance_uid, series_instance_uid, dicom_dir
            )
            nifti_path = await asyncio.get_event_loop().run_in_executor(
                None,
                self._convert_dicom_to_nifti,
                dicom_dir,
                output_path,
            )
        return nifti_path

    @staticmethod
    def _convert_dicom_to_nifti(dicom_dir: str, output_path: str) -> str:
        reader = sitk.ImageSeriesReader()
        dicom_files = reader.GetGDCMSeriesFileNames(dicom_dir)
        if not dicom_files:
            raise ValueError(f"No DICOM files found in {dicom_dir}")
        reader.SetFileNames(dicom_files)
        reader.MetaDataDictionaryArrayUpdateOn()
        reader.LoadPrivateTagsOn()
        image = reader.Execute()
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        sitk.WriteImage(image, output_path)
        logger.info("converted_dicom_to_nifti", output=output_path)
        return output_path

    async def upload_dicom_instance(self, dicom_bytes: bytes) -> str:
        """Upload a DICOM instance to Orthanc via POST /instances. Returns the Orthanc instance ID."""
        response = await self._client.post(
            "/instances",
            content=dicom_bytes,
            headers={"Content-Type": "application/dicom"},
        )
        response.raise_for_status()
        result = response.json()
        instance_id = result.get("ID", result.get("ParentSeries", "unknown"))
        logger.info("uploaded_dicom_instance", orthanc_id=instance_id)
        return instance_id

    async def close(self):
        await self._client.aclose()

    def extract_dicom_tag(self, metadata: dict[str, Any], tag: str) -> str | None:
        tag_data = metadata.get(tag)
        if tag_data and isinstance(tag_data, dict):
            value = tag_data.get("Value")
            if value and isinstance(value, list):
                first = value[0]
                if isinstance(first, dict):
                    return first.get("Alphabetic", str(first))
                return str(first)
        return None
