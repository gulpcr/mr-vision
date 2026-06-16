from __future__ import annotations

import hashlib
import re
from typing import Any

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

# DICOM tags that may contain PHI
PHI_TAGS = [
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "ReferringPhysicianName",
    "InstitutionName",
    "InstitutionAddress",
    "StationName",
    "PerformingPhysicianName",
    "OperatorsName",
    "OtherPatientIDs",
    "OtherPatientNames",
    "MedicalRecordLocator",
    "EthnicGroup",
    "Occupation",
    "AdditionalPatientHistory",
    "PatientComments",
    "RequestingPhysician",
    "ScheduledPerformingPhysicianName",
]

# Tags to keep but hash
HASH_TAGS = [
    "PatientName",
    "PatientID",
    "AccessionNumber",
    "ReferringPhysicianName",
]

# Tags to completely remove
REMOVE_TAGS = [
    "PatientBirthDate",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "InstitutionAddress",
    "MedicalRecordLocator",
    "OtherPatientIDs",
    "OtherPatientNames",
    "PatientComments",
    "AdditionalPatientHistory",
]


class PHIScrubber:
    """De-identifies PHI from DICOM metadata."""

    def __init__(self):
        settings = get_settings()
        self._method = settings.phi_deidentify_method
        self._salt = settings.phi_hash_salt

    def scrub_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Remove or hash PHI fields from DICOM metadata dict."""
        cleaned = dict(metadata)

        for tag in REMOVE_TAGS:
            cleaned.pop(tag, None)

        if self._method == "hash":
            for tag in HASH_TAGS:
                if tag in cleaned and cleaned[tag]:
                    cleaned[tag] = self._hash_value(str(cleaned[tag]))
        elif self._method == "remove":
            for tag in PHI_TAGS:
                cleaned.pop(tag, None)
        elif self._method == "replace":
            for tag in PHI_TAGS:
                if tag in cleaned:
                    cleaned[tag] = f"DEIDENTIFIED_{tag}"

        return cleaned

    def scrub_study_fields(self, study_data: dict[str, Any]) -> dict[str, Any]:
        """De-identify study-level fields."""
        cleaned = dict(study_data)

        field_map = {
            "patient_name": "PatientName",
            "patient_id": "PatientID",
            "referring_physician": "ReferringPhysicianName",
            "institution_name": "InstitutionName",
            "accession_number": "AccessionNumber",
        }

        if self._method == "hash":
            for field, _ in field_map.items():
                if field in cleaned and cleaned[field]:
                    cleaned[field] = self._hash_value(str(cleaned[field]))
        elif self._method == "remove":
            for field in field_map:
                cleaned[field] = None
        elif self._method == "replace":
            for field in field_map:
                if field in cleaned and cleaned[field]:
                    cleaned[field] = f"DEIDENTIFIED"

        return cleaned

    def _hash_value(self, value: str) -> str:
        """Hash a value with salt for consistent pseudonymization."""
        salted = f"{self._salt}:{value}"
        return hashlib.sha256(salted.encode()).hexdigest()[:16]

    def is_phi_clean(self, metadata: dict[str, Any]) -> bool:
        """Check if metadata has been de-identified."""
        for tag in REMOVE_TAGS:
            if tag in metadata and metadata[tag]:
                return False
        return True
