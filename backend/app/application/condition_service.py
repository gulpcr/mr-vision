"""ConditionService — the only writer of the ``conditions`` table (FHIR Condition).

Turns the referral reason captured at intake into a coded, queryable diagnosis. The
narrative stays where it was (``orders.indication`` / ``clinical_history``, and in
``conditions.note``); this adds the coded form beside it, so nothing that reads the prose
breaks and nothing is lost in translation.

**Coding is caller-supplied and never inferred.** Intake accepts an explicit
``{system, code, display}``; free text is *not* auto-coded. Guessing ICD-10 from prose is
exactly the failure this work exists to prevent — a wrong diagnosis code is filed by the
receiving system as fact, with nothing to surface the error. An order with only free text
produces no Condition, which is the honest outcome.

Idempotent by source, like ObservationService: a re-save replaces exactly this order's own
rows rather than accumulating contradictory diagnoses for one referral.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ConditionCategory,
    ConditionClinicalStatus,
    ConditionVerificationStatus,
)
from app.domain.fhir_terminology import ICD10_CM_SYSTEM, ICD10_SYSTEM, SNOMED_SYSTEM
from app.domain.models import utcnow
from app.infrastructure.database.models import ConditionRecord

logger = structlog.get_logger(__name__)

# Only recognised diagnosis code systems are accepted. An unknown system URI is refused
# rather than stored, because a code whose system we cannot name is not interpretable by
# the receiving end — the same rule identifiers follow in domain/fhir_terminology.py.
ALLOWED_CODE_SYSTEMS: frozenset[str] = frozenset(
    {ICD10_SYSTEM, ICD10_CM_SYSTEM, SNOMED_SYSTEM}
)


class ConditionValidationError(ValueError):
    """A supplied diagnosis code cannot be stored as given (→ 422)."""


class ConditionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_order_condition(
        self,
        order: Any,
        patient_ref: str,
        code_system: str | None,
        code: str | None,
        code_display: str | None = None,
        clinical_status: str | None = None,
        verification_status: str | None = None,
        onset_dt: datetime | None = None,
        tenant_id: str = "default",
    ) -> int:
        """(Re)derive the coded Condition for one intake order.

        Returns the number of rows written — 0 when no code was supplied, which is the
        normal case for a free-text-only referral and is not an error.
        """
        source = f"order:{getattr(order, 'id', '')}"
        if not patient_ref or not getattr(order, "id", None):
            return 0

        system = (code_system or "").strip()
        value = (code or "").strip()
        if not value:
            # No code supplied: drop any previously derived row (the coding may have been
            # cleared on edit) and record nothing new.
            return await self._replace(source, [])

        if system not in ALLOWED_CODE_SYSTEMS:
            raise ConditionValidationError(
                f"unrecognised diagnosis code system {system!r}; expected one of "
                f"{sorted(ALLOWED_CODE_SYSTEMS)}"
            )

        try:
            status = ConditionClinicalStatus(
                (clinical_status or ConditionClinicalStatus.ACTIVE.value).strip().lower()
            )
            verification = ConditionVerificationStatus(
                (verification_status or ConditionVerificationStatus.UNCONFIRMED.value)
                .strip()
                .lower()
            )
        except ValueError as exc:
            raise ConditionValidationError(str(exc)) from exc

        note = (
            getattr(order, "clinical_history", None)
            or getattr(order, "indication", None)
            or None
        )
        row = ConditionRecord(
            tenant_id=tenant_id or "default",
            patient_ref=patient_ref,
            patient_id=getattr(order, "patient_id", None),
            code_system=system,
            code=value,
            code_display=(code_display or "").strip(),
            # An imaging referral reason is an encounter diagnosis, not a problem-list item.
            category=ConditionCategory.ENCOUNTER_DIAGNOSIS.value,
            clinical_status=status.value,
            verification_status=verification.value,
            onset_dt=onset_dt,
            recorded_dt=utcnow(),
            note=note,
            # SET NULL on the FK, so purging the study leaves the diagnosis intact.
            study_instance_uid=getattr(order, "study_instance_uid", None),
            order_id=getattr(order, "id", None),
            source=source,
        )
        return await self._replace(source, [row])

    async def search(
        self,
        patient_ref: str | None = None,
        code: str | None = None,
        clinical_status: str | None = None,
        category: str | None = None,
        tenant_id: str = "default",
        limit: int = 200,
    ) -> list[ConditionRecord]:
        """Condition search axes: patient, code, clinical-status, category."""
        stmt = select(ConditionRecord).where(ConditionRecord.tenant_id == tenant_id)
        if patient_ref:
            stmt = stmt.where(ConditionRecord.patient_ref == patient_ref)
        if code:
            stmt = stmt.where(ConditionRecord.code == code)
        if clinical_status:
            stmt = stmt.where(ConditionRecord.clinical_status == clinical_status)
        if category:
            stmt = stmt.where(ConditionRecord.category == category)
        stmt = stmt.order_by(ConditionRecord.recorded_dt.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _replace(self, source: str, rows: list[ConditionRecord]) -> int:
        await self._session.execute(
            delete(ConditionRecord).where(ConditionRecord.source == source)
        )
        for row in rows:
            self._session.add(row)
        await self._session.flush()
        if rows:
            logger.info("conditions_recorded", source=source, count=len(rows))
        return len(rows)
