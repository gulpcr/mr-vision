from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    AuditLogRecord,
    MammographyReportRecord,
    StudyRecord,
)

# Editable report fields (everything the radiologist can set/override).
EDITABLE_FIELDS = [
    "laterality",
    "file_no",
    "status",
    "contact",
    "procedure",
    "clinical_features",
    "right_breast_findings",
    "left_breast_findings",
    "opinion",
    "birads_right",
    "birads_left",
    "reviewing_doctor",
    "reporting_doctor",
]

_BIRADS_VALUES = {"0", "1", "2", "3", "4", "5", "6"}


class StudyNotFoundError(Exception):
    """Raised when the target study does not exist."""


class ReportValidationError(Exception):
    """Raised when a submitted field is invalid (e.g. bad BI-RADS)."""


class MammographyService:
    """Get / upsert the radiologist-authored mammography report for a study."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_report(self, study_uid: str) -> dict[str, Any] | None:
        rec = await self._session.get(MammographyReportRecord, study_uid)
        return self._to_dict(rec) if rec else None

    async def upsert_report(
        self, study_uid: str, payload: dict[str, Any], actor_id: str, tenant_id: str
    ) -> dict[str, Any]:
        study = await self._session.get(StudyRecord, study_uid)
        if study is None:
            raise StudyNotFoundError(study_uid)

        for key in ("birads_right", "birads_left"):
            val = payload.get(key)
            if val not in (None, "") and str(val) not in _BIRADS_VALUES:
                raise ReportValidationError(f"{key} must be one of 0-6")

        rec = await self._session.get(MammographyReportRecord, study_uid)
        created = rec is None
        if rec is None:
            rec = MammographyReportRecord(
                study_instance_uid=study_uid,
                tenant_id=tenant_id or "default",
                created_by=actor_id or None,
            )
            self._session.add(rec)

        for key in EDITABLE_FIELDS:
            if key in payload:
                value = payload[key]
                setattr(rec, key, value if value not in ("",) else None)

        await self._session.flush()
        await self._audit(
            actor_id, "mammography_report_saved", study_uid, {"created": created}
        )
        return self._to_dict(rec)

    async def _audit(
        self, actor: str | None, action: str, entity_id: str, details: dict[str, Any]
    ) -> None:
        self._session.add(
            AuditLogRecord(
                id=str(uuid.uuid4()),
                action=action,
                entity_type="mammography_report",
                entity_id=entity_id,
                actor=actor or "system",
                details=details,
            )
        )

    @staticmethod
    def _to_dict(rec: MammographyReportRecord) -> dict[str, Any]:
        data: dict[str, Any] = {f: getattr(rec, f) for f in EDITABLE_FIELDS}
        data["study_instance_uid"] = rec.study_instance_uid
        data["created_at"] = rec.created_at.isoformat() if rec.created_at else None
        data["updated_at"] = rec.updated_at.isoformat() if rec.updated_at else None
        return data
