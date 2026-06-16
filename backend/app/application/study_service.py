from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

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

        study_date_raw = ext(study_meta, "StudyDate")
        study_date = None
        if study_date_raw:
            try:
                study_date = datetime.strptime(str(study_date_raw), "%Y%m%d")
            except (ValueError, TypeError):
                pass

        study = Study(
            study_instance_uid=study_instance_uid,
            patient_id=ext(study_meta, "PatientID"),
            patient_name=ext(study_meta, "PatientName"),
            study_date=study_date,
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
