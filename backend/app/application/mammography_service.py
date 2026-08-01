from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditActorType, audit_action_to_crude
from app.infrastructure.database.models import (
    AuditLogRecord,
    MammographyReportRecord,
    StudyRecord,
)

logger = structlog.get_logger(__name__)


def _audit_actor_fields(actor: str | None) -> dict[str, str | None]:
    """Type a legacy free-text ``actor`` for the AuditEvent columns.

    Callers pass a user id, a username, or a machine name depending on history. A value
    that is neither a known machine actor nor plainly an id is still recorded in
    ``actor_display`` but leaves ``actor_id`` NULL — an unattributed entry beats a
    misattributed one. Fix the call site to pass a stable id and both populate.
    """
    raw = (actor or "system").strip() or "system"
    machine = raw in ("system", "celery_worker")
    return {
        "actor": raw,
        "actor_type": (
            AuditActorType.SYSTEM.value if machine else AuditActorType.PRACTITIONER.value
        ),
        "actor_id": None if machine else raw,
        "actor_display": raw,
    }


# Structured per-breast finding slots and their allowed values (None always allowed).
# density: BI-RADS breast composition a-d; presence slots: none|present; nodes: normal|abnormal.
_SLOT_VALUES: dict[str, set[str]] = {}
for _side in ("right", "left"):
    _SLOT_VALUES[f"density_{_side}"] = {"a", "b", "c", "d"}
    for _slot in (
        "mass",
        "calcification",
        "skin_thickening",
        "nipple_retraction",
        "architectural_distortion",
    ):
        _SLOT_VALUES[f"{_slot}_{_side}"] = {"none", "present"}
    _SLOT_VALUES[f"axillary_nodes_{_side}"] = {"normal", "abnormal"}

STRUCTURED_SLOT_FIELDS = list(_SLOT_VALUES.keys())

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
    *STRUCTURED_SLOT_FIELDS,
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

        for key, allowed in _SLOT_VALUES.items():
            val = payload.get(key)
            if val not in (None, "") and str(val) not in allowed:
                raise ReportValidationError(
                    f"{key} must be one of {sorted(allowed)}"
                )

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
        # PRE-EXISTING BUG FIX (independent of the Observation work below).
        # updated_at is `onupdate=func.now()`, so a flush that actually changes a field
        # issues `SET updated_at = now()` and SQLAlchemy expires the attribute — it cannot
        # know the server-computed value. _to_dict then reads rec.updated_at, which
        # triggers implicit IO; on an AsyncSession that raises MissingGreenlet and the save
        # returns 500. It only *looked* fine before because a save that changes nothing
        # emits no UPDATE and so expires nothing — i.e. editing a report failed while
        # re-saving an unchanged one succeeded. refresh() fetches the server values once,
        # explicitly, instead of relying on a lazy load that async cannot service.
        await self._session.refresh(rec)
        # Materialise the response BEFORE the derived-data hook. The hook shares this
        # session and writes through it, which can expire `rec`'s attributes again;
        # building the payload first makes the caller immune to whatever the hook does.
        payload = self._to_dict(rec)
        await self._record_observations(rec, study)
        await self._audit(
            actor_id, "mammography_report_saved", study_uid, {"created": created}
        )
        return payload

    async def _record_observations(
        self, rec: MammographyReportRecord, study: StudyRecord
    ) -> None:
        """Pivot the saved per-breast slots into Observation rows (step 4, source 3).

        Best-effort: deriving a searchable copy of a finding must never fail the
        radiologist's save. Idempotent — ObservationService replaces this study's own
        mammography rows, which matters because these slots are edited repeatedly.
        """
        from app.config import get_settings

        if not get_settings().observations_enabled:
            return
        patient_ref = getattr(study, "patient_id", None)
        if not patient_ref:
            return
        try:
            from app.application.observation_service import ObservationService

            # SAVEPOINT, not just try/except. These hooks share the caller's session, so a
            # failure inside one can leave the session unusable and take the radiologist's
            # report save down with it — catching the exception is not sufficient on its
            # own. begin_nested() rolls back only the hook's work and leaves the outer
            # transaction intact, which is what "must never fail the caller" requires.
            async with self._session.begin_nested():
                await ObservationService(self._session).record_mammography_observations(
                    rec,
                    patient_ref=patient_ref,
                    tenant_id=getattr(rec, "tenant_id", None) or "default",
                )
        except Exception as exc:
            logger.warning(
                "mammography_observations_failed",
                study_uid=getattr(rec, "study_instance_uid", None),
                error=str(exc),
            )

    async def _audit(
        self, actor: str | None, action: str, entity_id: str, details: dict[str, Any]
    ) -> None:
        self._session.add(
            AuditLogRecord(
                id=str(uuid.uuid4()),
                action=action,
                entity_type="mammography_report",
                entity_id=entity_id,
                **_audit_actor_fields(actor),
                action_crude=audit_action_to_crude(action),
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
