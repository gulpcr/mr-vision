from __future__ import annotations

"""Apply the Safe Harbor boundary (domain/deidentification.py) to what pipelines receive.

``DeidentifyingPACSClient`` wraps the PACS client a Celery task hands to a pipeline: every
DICOM file is rewritten de-identified the moment it is downloaded, before the pipeline (or
SimpleITK's NIfTI conversion) reads it, and the client refuses PACS writes and whole-study
archives. ``deidentified_study`` gives the pipeline a Study without name or MRN. The task
itself keeps the identified record to link results back.
"""

import asyncio
import datetime as dt
import hashlib
import os
import tempfile
from dataclasses import replace
from typing import Any

import pydicom
import structlog
from pydicom.dataset import Dataset

from app.domain.deidentification import (
    BLANKED_TAGS,
    DEIDENTIFICATION_METHOD,
    PSEUDONYMISED_TAGS,
    REMOVED_TAGS,
    cap_age,
)
from app.domain.interfaces import PACSClient
from app.domain.models import Study

logger = structlog.get_logger(__name__)


def pseudonym(value: str, salt: str) -> str:
    """Stable, non-reversible stand-in for an identifier (same patient → same pseudonym)."""
    return "ANON-" + hashlib.sha256(f"{salt}:deid:{value}".encode()).hexdigest()[:12].upper()


def _parse_da(value: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(value)[:8], "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _date_offset(ds: Dataset) -> dt.timedelta | None:
    """Shift that moves the study date to 1 January of its year."""
    for keyword in ("StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"):
        anchor = _parse_da(ds.get(keyword, "") or "")
        if anchor:
            return dt.date(anchor.year, 1, 1) - anchor
    return None


def _shift_dates(ds: Dataset, offset: dt.timedelta | None) -> None:
    for elem in ds:
        if elem.VR == "SQ":
            for item in elem.value or []:
                _shift_dates(item, offset)
            continue
        if elem.VR not in ("DA", "DT") or not elem.value:
            continue
        values = elem.value if elem.VM > 1 else [elem.value]
        shifted = []
        for v in values:
            text = str(v)
            day = _parse_da(text)
            if day is None or offset is None:
                shifted.append("")  # unparseable / no anchor: drop the date
                continue
            shifted.append((day + offset).strftime("%Y%m%d") + text[8:])
        elem.value = shifted if elem.VM > 1 else shifted[0]


def _scrub(ds: Dataset, salt: str) -> None:
    for keyword in REMOVED_TAGS:
        if keyword in ds:
            delattr(ds, keyword)
    for keyword in BLANKED_TAGS:
        if keyword in ds:
            setattr(ds, keyword, "")
    for keyword in PSEUDONYMISED_TAGS:
        if keyword in ds and ds.get(keyword):
            setattr(ds, keyword, pseudonym(str(ds.get(keyword)), salt))
    if "PatientAge" in ds:
        ds.PatientAge = cap_age(str(ds.PatientAge)) or ""
    for elem in list(ds):
        if elem.VR == "PN" and elem.value:  # any other person name, anywhere
            elem.value = ""
        elif elem.VR == "SQ":
            for item in elem.value or []:
                _scrub(item, salt)


def deidentify_dataset(ds: Dataset, salt: str) -> Dataset:
    """In place: Safe Harbor boundary applied to one DICOM dataset (see module docs)."""
    offset = _date_offset(ds)
    ds.remove_private_tags()
    _scrub(ds, salt)
    _shift_dates(ds, offset)
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = DEIDENTIFICATION_METHOD[:64]
    return ds


def deidentify_file(path: str, salt: str, redaction=None) -> None:
    """De-identify one file in place; with ``redaction`` (a burned_in.RedactionReport) also
    mask text burned into the pixels (raises BurnedInTextUnverifiable if it cannot)."""
    ds = pydicom.dcmread(path, force=True)
    deidentify_dataset(ds, salt)
    if redaction is not None:
        from app.infrastructure.orthanc.burned_in import redact_dataset

        redact_dataset(ds, redaction)
    ds.save_as(path, write_like_original=True)


def deidentified_study(study: Study, salt: str) -> Study:
    """The Study a pipeline is given: no name, MRN pseudonymised, no dates or staff."""
    return replace(
        study,
        patient_id=pseudonym(study.patient_id, salt) if study.patient_id else None,
        patient_record_id=None,
        patient_name=None,
        patient_age=cap_age(study.patient_age),
        study_date=None,
        accession_number=None,
        referring_physician=None,
        institution_name=None,
        assigned_to=None,
        assigned_to_username=None,
    )


_PATIENT_JSON_TAGS = {
    "00100010", "00100020", "00100030", "00101000", "00101001", "00101040", "00102154",
    "00080050", "00080080", "00080090", "00081050", "00081070", "00200010",
}


def _scrub_json(node: Any) -> Any:
    if isinstance(node, list):
        return [_scrub_json(n) for n in node]
    if isinstance(node, dict):
        return {k: _scrub_json(v) for k, v in node.items()
                if k not in _PATIENT_JSON_TAGS and k not in ("PatientMainDicomTags",)}
    return node


class DeidentifyingPACSClient(PACSClient):
    """Read-only, de-identifying view of the PACS for pipeline code."""

    def __init__(self, inner: PACSClient, salt: str, check_burned_in_text: bool = True):
        from app.infrastructure.orthanc.burned_in import RedactionReport

        self._inner = inner
        self._salt = salt
        # Pixel text check (burned_in.py); the task reads the report into QA flags.
        self.redaction = RedactionReport() if check_burned_in_text else None

    async def get_study(self, study_instance_uid: str) -> dict[str, Any]:
        return _scrub_json(await self._inner.get_study(study_instance_uid))

    async def get_series_list(self, study_instance_uid: str) -> list[dict[str, Any]]:
        return _scrub_json(await self._inner.get_series_list(study_instance_uid))

    async def download_series_dicoms(
        self, study_instance_uid: str, series_instance_uid: str, output_dir: str
    ) -> list[str]:
        paths = await self._inner.download_series_dicoms(
            study_instance_uid, series_instance_uid, output_dir
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._deidentify_all, paths)
        return paths

    def _deidentify_all(self, paths: list[str]) -> None:
        for p in paths:
            deidentify_file(p, self._salt, self.redaction)

    async def download_series_as_nifti(
        self, study_instance_uid: str, series_instance_uid: str, output_path: str
    ) -> str:
        from app.infrastructure.orthanc.client import OrthancPACSClient

        # De-identify the DICOM files before SimpleITK converts them (the NIfTI itself
        # carries no patient tags, but the intermediate files must not either).
        with tempfile.TemporaryDirectory() as tmpdir:
            dicom_dir = os.path.join(tmpdir, "dicoms")
            await self.download_series_dicoms(study_instance_uid, series_instance_uid, dicom_dir)
            return await asyncio.get_event_loop().run_in_executor(
                None, OrthancPACSClient._convert_dicom_to_nifti, dicom_dir, output_path,
            )

    async def get_series_instance_geometry(self, *args, **kwargs):
        return await self._inner.get_series_instance_geometry(*args, **kwargs)

    # A pipeline never writes to, deletes from or exports from the PACS.
    async def upload_dicom_instance(self, dicom_bytes: bytes) -> str:
        raise PermissionError("pipelines may not write to the PACS")

    async def download_study_archive(self, study_instance_uid: str) -> bytes:
        raise PermissionError("pipelines may not download identified study archives")

    async def delete_study_by_uid(self, study_instance_uid: str) -> bool:
        raise PermissionError("pipelines may not delete studies")

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()
