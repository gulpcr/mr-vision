from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from app.application.dicom_demographics import normalize_administrative_gender
from app.domain.enums import AuditAction, BodyPart
from app.domain.interfaces import (
    AuditRepository,
    PACSClient,
    SeriesRepository,
    StudyRepository,
)
from app.domain.models import AuditEntry, Series, Study
from app.infrastructure.dicomweb.client import DICOMwebClient

logger = structlog.get_logger(__name__)


def _parse_study_datetime(
    study_date_raw: Any, study_time_raw: Any
) -> tuple[datetime | None, str]:
    """Combine DICOM StudyDate (0008,0020) and StudyTime (0008,0030) into one instant.

    Returns ``(datetime | None, precision)`` where precision is ``"second"`` if
    StudyTime was present and parsed, else ``"date"``. Reading StudyDate alone — as
    this did previously — silently produces midnight for every study, which is
    indistinguishable from a study genuinely acquired at 00:00 and makes two studies
    on the same day unorderable by time.

    StudyTime is DICOM VR TM: ``HHMMSS.FFFFFF`` with any trailing component optional,
    so HH, HHMM and HHMMSS all occur in the wild along with an optional fraction.

    Timezone: DICOM StudyDate/StudyTime are *site-local* wall-clock unless
    TimezoneOffsetFromUTC (0008,0201) is present, which it usually is not. Every other
    timestamp in this platform is UTC, and the pre-existing behaviour was to store this
    value unlabelled alongside them, so it is labelled UTC here to keep one consistent
    convention. That is an assumption, not a fact: for a site not operating in UTC the
    time-of-day is offset. Reading 0008,0201 when present is the correct refinement.
    """
    if not study_date_raw:
        return None, "date"

    try:
        study_date = datetime.strptime(str(study_date_raw), "%Y%m%d")
    except (ValueError, TypeError):
        return None, "date"

    raw_time = str(study_time_raw or "").strip()
    if raw_time:
        # Drop the optional fractional seconds, then pad HH / HHMM out to HHMMSS.
        digits = raw_time.split(".")[0]
        if digits.isdigit() and len(digits) in (2, 4, 6):
            try:
                parsed = datetime.strptime(digits.ljust(6, "0"), "%H%M%S")
                study_date = study_date.replace(
                    hour=parsed.hour, minute=parsed.minute, second=parsed.second
                )
                return study_date.replace(tzinfo=timezone.utc), "second"
            except (ValueError, TypeError):
                pass
        logger.warning("study_time_unparseable", study_time=raw_time)

    return study_date.replace(tzinfo=timezone.utc), "date"


class StudyService:
    """Handles study ingestion from Orthanc and metadata synchronization."""

    def __init__(
        self,
        study_repo: StudyRepository,
        series_repo: SeriesRepository,
        audit_repo: AuditRepository,
        pacs_client: PACSClient,
        dicomweb_client: DICOMwebClient,
    ):
        self._study_repo = study_repo
        self._series_repo = series_repo
        self._audit_repo = audit_repo
        self._pacs = pacs_client
        self._dw = dicomweb_client

    async def ingest_study(self, study_instance_uid: str) -> Study:
        """Fetch study metadata from Orthanc and persist it."""
        existing = await self._study_repo.get_by_uid(study_instance_uid)
        if existing:
            logger.info("study_already_ingested", study_uid=study_instance_uid)
            return existing

        study_meta = await self._pacs.get_study(study_instance_uid)
        ext = DICOMwebClient.extract_tag_value

        body_part_raw = ext(study_meta, "BodyPartExamined") or ""
        body_part = None
        try:
            body_part = BodyPart(body_part_raw.upper())
        except ValueError:
            pass

        study_date, study_date_precision = _parse_study_datetime(
            ext(study_meta, "StudyDate"), ext(study_meta, "StudyTime")
        )

        # Patient demographics for reporting (PatientWeight in kg, PatientSize in m)
        def _to_float(raw: Any) -> float | None:
            try:
                return float(raw) if raw not in (None, "") else None
            except (ValueError, TypeError):
                return None

        weight_kg = _to_float(ext(study_meta, "PatientWeight"))
        size_m = _to_float(ext(study_meta, "PatientSize"))
        height_cm = round(size_m * 100.0, 1) if size_m else None

        # Patient age: prefer the DICOM PatientAge tag (0010,1010); if absent (common — many
        # scanners omit it), compute it from PatientBirthDate (0010,0030) + StudyDate so age
        # still flows to the reports. Stored in DICOM AS format ("022Y") for downstream parsing.
        #
        # PatientBirthDate is read here and then DISCARDED — never persisted. That is a
        # deliberate PHI decision (PatientRecord is de-identified: sex + age band only),
        # with a knowingly accepted cost: Patient.birthDate can never be exported to FHIR
        # and Patient?birthdate= search is unavailable. age_band is NOT a substitute —
        # FHIR has no age element. See app/application/dicom_demographics.py.
        patient_age = ext(study_meta, "PatientAge")
        if not patient_age:
            birth_raw = ext(study_meta, "PatientBirthDate")
            if birth_raw and study_date:
                try:
                    bdt = datetime.strptime(str(birth_raw), "%Y%m%d")
                    years = study_date.year - bdt.year - (
                        (study_date.month, study_date.day) < (bdt.month, bdt.day)
                    )
                    if 0 <= years < 130:
                        patient_age = f"{years:03d}Y"
                except (ValueError, TypeError):
                    pass

        study = Study(
            study_instance_uid=study_instance_uid,
            patient_id=ext(study_meta, "PatientID"),
            patient_name=ext(study_meta, "PatientName"),
            # Normalised at the boundary to the FHIR administrativeGender value set, so
            # studies.patient_sex and patients.sex finally agree (both "male"/"female"/
            # "other"). Raw DICOM "M"/"F"/"O" is not a valid administrativeGender code.
            patient_sex=normalize_administrative_gender(ext(study_meta, "PatientSex")),
            patient_age=patient_age,
            patient_weight_kg=weight_kg,
            patient_height_cm=height_cm,
            study_date=study_date,
            study_date_precision=study_date_precision,
            study_description=ext(study_meta, "StudyDescription"),
            accession_number=ext(study_meta, "AccessionNumber"),
            referring_physician=ext(study_meta, "ReferringPhysicianName"),
            body_part_examined=body_part,
            modality=ext(study_meta, "Modality"),
            institution_name=ext(study_meta, "InstitutionName"),
        )

        try:
            await self._study_repo.save(study)
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                logger.info("study_already_exists_race", study_uid=study_instance_uid)
                existing = await self._study_repo.get_by_uid(study_instance_uid)
                if existing:
                    return existing
            raise
        logger.info("study_ingested", study_uid=study_instance_uid)
        try:
            from app.infrastructure.metrics import STUDY_INGESTED_TOTAL
            STUDY_INGESTED_TOTAL.inc()
        except Exception:
            pass

        series_list_raw = await self._pacs.get_series_list(study_instance_uid)
        series_objects = []
        for s in series_list_raw:
            px_spacing = ext(s, "PixelSpacing")
            # Extract num_instances
            num_inst_raw = ext(s, "NumberOfSeriesRelatedInstances")
            num_instances = 0
            if num_inst_raw is not None:
                try:
                    num_instances = int(num_inst_raw)
                except (ValueError, TypeError):
                    pass

            series_obj = Series(
                series_instance_uid=ext(s, "SeriesInstanceUID") or "",
                study_instance_uid=study_instance_uid,
                series_number=ext(s, "SeriesNumber"),
                series_description=ext(s, "SeriesDescription"),
                modality=ext(s, "Modality"),
                body_part_examined=ext(s, "BodyPartExamined"),
                protocol_name=ext(s, "ProtocolName"),
                num_instances=num_instances,
                slice_thickness=ext(s, "SliceThickness"),
                dicom_tags={
                    "RepetitionTime": ext(s, "RepetitionTime"),
                    "EchoTime": ext(s, "EchoTime"),
                    "InversionTime": ext(s, "InversionTime"),
                    "MagneticFieldStrength": ext(s, "MagneticFieldStrength"),
                    "FlipAngle": ext(s, "FlipAngle"),
                    "ScanningSequence": ext(s, "ScanningSequence"),
                    "SequenceVariant": ext(s, "SequenceVariant"),
                    "MRAcquisitionType": ext(s, "MRAcquisitionType"),
                    "SequenceName": ext(s, "SequenceName"),
                    "Rows": ext(s, "Rows"),
                    "Columns": ext(s, "Columns"),
                },
            )
            series_objects.append(series_obj)

        await self._series_repo.save_many(series_objects)
        study.series = series_objects

        # Infer study-level body_part and modality from series if missing
        if not study.body_part_examined and series_objects:
            for so in series_objects:
                if so.body_part_examined:
                    try:
                        study.body_part_examined = BodyPart(so.body_part_examined.upper())
                    except ValueError:
                        pass
                    break
        if not study.modality and series_objects:
            for so in series_objects:
                if so.modality:
                    study.modality = so.modality
                    break
        if study.body_part_examined or study.modality:
            await self._study_repo.save(study)

        await self._audit_repo.save(
            AuditEntry(
                action=AuditAction.STUDY_RECEIVED,
                entity_type="study",
                entity_id=study_instance_uid,
                details={
                    "patient_id": study.patient_id,
                    "series_count": len(series_objects),
                },
            )
        )

        return study

    async def get_study(self, study_instance_uid: str) -> Study | None:
        study = await self._study_repo.get_by_uid(study_instance_uid)
        if study:
            study.series = await self._series_repo.list_by_study(study_instance_uid)
        return study

    async def list_studies(
        self, offset: int = 0, limit: int = 50, filters: dict[str, Any] | None = None
    ) -> tuple[list[Study], int]:
        studies = await self._study_repo.list_studies(offset, limit, filters)
        total = await self._study_repo.count(filters)
        return studies, total

    async def upload_and_ingest(
        self, files: list[tuple[str, bytes]]
    ) -> dict[str, Any]:
        """Push raw DICOM files into Orthanc, then ingest every distinct study.

        ``files`` is a list of ``(filename, dicom_bytes)`` — already read from the
        request in the interface layer so no FastAPI type leaks in here. Each file is
        parsed locally for its StudyInstanceUID (so we know what to ingest) before
        being uploaded; a file that is not readable DICOM is recorded as failed and
        skipped rather than aborting the whole batch. Once all files are stored, each
        unique study is ingested through the same path as PACS/Orthanc ingest, so it
        lands in the platform identically and is ready for AI routing.
        """
        from io import BytesIO

        import pydicom

        file_results: list[dict[str, Any]] = []
        study_uids: set[str] = set()
        uploaded = 0
        failed = 0

        for filename, data in files:
            try:
                ds = pydicom.dcmread(
                    BytesIO(data), stop_before_pixels=True, force=True
                )
                study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
            except Exception as exc:
                file_results.append(
                    {"filename": filename, "status": "error",
                     "detail": f"Not a readable DICOM file: {exc}"}
                )
                failed += 1
                continue

            if not study_uid:
                file_results.append(
                    {"filename": filename, "status": "skipped",
                     "detail": "No StudyInstanceUID — not a DICOM image (e.g. DICOMDIR)."}
                )
                failed += 1
                continue

            try:
                orthanc_id = await self._pacs.upload_dicom_instance(data)
            except Exception as exc:
                file_results.append(
                    {"filename": filename, "status": "error",
                     "detail": f"PACS rejected the file: {exc}"}
                )
                failed += 1
                continue

            study_uids.add(study_uid)
            file_results.append(
                {"filename": filename, "status": "uploaded",
                 "orthanc_id": orthanc_id, "study_instance_uid": study_uid}
            )
            uploaded += 1

        studies_ingested: list[dict[str, Any]] = []
        for uid in study_uids:
            try:
                study = await self.ingest_study(uid)
                studies_ingested.append(
                    {"study_instance_uid": study.study_instance_uid,
                     "patient_name": study.patient_name,
                     "patient_id": study.patient_id,
                     "modality": study.modality,
                     "series_count": len(study.series or [])}
                )
            except Exception as exc:
                logger.warning("upload_ingest_failed", study_uid=uid, error=str(exc))
                studies_ingested.append(
                    {"study_instance_uid": uid, "error": str(exc)}
                )

        logger.info(
            "dicom_upload_batch",
            files=len(files), uploaded=uploaded, failed=failed,
            studies=len(studies_ingested),
        )
        return {
            "uploaded": uploaded,
            "failed": failed,
            "studies_ingested": studies_ingested,
            "files": file_results,
        }
