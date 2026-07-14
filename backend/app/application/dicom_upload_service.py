from __future__ import annotations

from io import BytesIO

import structlog

from app.domain.enums import AuditAction
from app.domain.interfaces import AuditRepository, PACSClient, PendingStudyTenantRepository, StudyRepository
from app.domain.models import AuditEntry, PendingStudyTenant

logger = structlog.get_logger(__name__)


class DicomUploadService:
    """Tenant-scoped DICOM ingress gateway.

    DICOM images normally land in Orthanc via the DICOM protocol, which has no
    concept of tenant. This service is the one path where an external system,
    authenticated with a per-tenant API key, pushes an instance in through us:
    we push it into Orthanc on the tenant's behalf and record which tenant it
    belongs to *before* Orthanc's async notify-stable-study webhook fires, so
    StudyService.ingest_study() can stamp the right tenant instead of "default".
    """

    def __init__(
        self,
        pacs_client: PACSClient,
        pending_repo: PendingStudyTenantRepository,
        unscoped_study_repo: StudyRepository,
        audit_repo: AuditRepository,
    ):
        self._pacs = pacs_client
        self._pending_repo = pending_repo
        self._study_repo = unscoped_study_repo
        self._audit_repo = audit_repo

    async def upload_instance(
        self, tenant_id: str, api_key_id: str, dicom_bytes: bytes
    ) -> dict:
        study_instance_uid, sop_instance_uid = self._extract_uids(dicom_bytes)

        existing_study = await self._study_repo.get_by_uid(study_instance_uid)
        if existing_study is not None and existing_study.tenant_id != tenant_id:
            raise ValueError(
                f"Study {study_instance_uid} belongs to a different workspace"
            )

        await self._pending_repo.register(
            PendingStudyTenant(
                study_instance_uid=study_instance_uid,
                tenant_id=tenant_id,
                api_key_id=api_key_id,
            )
        )

        orthanc_id = await self._pacs.upload_dicom_instance(dicom_bytes)

        await self._audit_repo.save(
            AuditEntry(
                action=AuditAction.DICOM_UPLOADED,
                entity_type="study",
                entity_id=study_instance_uid,
                actor=f"api_key:{api_key_id}",
                tenant_id=tenant_id,
                details={
                    "sop_instance_uid": sop_instance_uid,
                    "orthanc_id": orthanc_id,
                },
            )
        )
        logger.info(
            "dicom_instance_uploaded",
            tenant_id=tenant_id,
            study_uid=study_instance_uid,
            orthanc_id=orthanc_id,
        )

        return {
            "study_instance_uid": study_instance_uid,
            "sop_instance_uid": sop_instance_uid,
            "orthanc_id": orthanc_id,
        }

    @staticmethod
    def _extract_uids(dicom_bytes: bytes) -> tuple[str, str | None]:
        import pydicom

        try:
            ds = pydicom.dcmread(BytesIO(dicom_bytes), stop_before_pixels=True)
        except Exception as exc:
            raise ValueError(f"Invalid DICOM file: {exc}") from exc

        study_instance_uid = getattr(ds, "StudyInstanceUID", None)
        if not study_instance_uid:
            raise ValueError("DICOM file is missing StudyInstanceUID")
        sop_instance_uid = getattr(ds, "SOPInstanceUID", None)
        return str(study_instance_uid), str(sop_instance_uid) if sop_instance_uid else None
