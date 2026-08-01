"""ObservationService — the only writer of the ``observations`` table.

Turns the platform's clinical values into row-per-concept FHIR Observations:

1. **Intake labs / vitals** from ``orders`` — weight, height, fasting glucose, creatinine.
2. **Mammography findings** from ``mammography_reports`` — per-breast slots pivoted out of
   column names into rows carrying an explicit laterality.
3. **AI measurements** from pipeline results, strictly per the plugin's ``fhir_map.yaml``
   whitelist (``application/fhir_terminology.py``). A plugin with no map exports nothing,
   which is what every narrative-only plugin relies on. A blanket loop over
   ``measurements`` can never be safe: the previous exporter published
   ``image_dimensions`` as a clinical finding while silently dropping SUVmax/MTV/TLG
   (FHIR_R4_COMPLIANCE_AUDIT.md §3.5).

Writes are **idempotent by source**: every row records which writer produced it
(``source`` = "order:{id}" / "mammography:{study_uid}"), and a re-save replaces exactly
that writer's rows. Report slots are edited repeatedly, so append-only would accumulate
contradictory findings for one study.

All callers invoke this inside ``try/except → logger.warning``: deriving an Observation
must never be able to fail an intake submission or a report save.

On codes: the four intake concepts below use LOINC codes that are stable and verifiable,
and the platform's UI and PET-CT report already fix both labs at mg/dL, so the units are
a settled deployment convention rather than an assumption. Mammography findings use a
LOCAL code system on purpose — BI-RADS and breast-density bindings need clinical sign-off
before anything is published externally (step 5), and a local system is honest in the
meantime: internally searchable, never wrongly asserting a standard concept.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import Laterality, ObservationCategory, ObservationStatus
# value_unit always holds a UCUM code (fhir_terminology.UCUM_SYSTEM is the system URI
# emitted alongside it at serialisation time).
from app.domain.fhir_terminology import LOCAL_OBSERVATION_SYSTEM, LOINC_SYSTEM
from app.domain.models import utcnow
from app.infrastructure.database.models import ObservationRecord

logger = structlog.get_logger(__name__)

# Single source of truth for the local system URI (domain layer, stdlib-only).
LOCAL_SYSTEM = LOCAL_OBSERVATION_SYSTEM


class _Concept:
    """A code + unit + category triple, with how confident we are in the code."""

    __slots__ = ("system", "code", "display", "unit", "category")

    def __init__(self, system: str, code: str, display: str, unit: str | None,
                 category: ObservationCategory) -> None:
        self.system = system
        self.code = code
        self.display = display
        self.unit = unit
        self.category = category


# ── Intake vitals / labs ──────────────────────────────────────────────────────
# LOINC codes verified and stable. Units are UCUM and match what the intake form and the
# PET-CT report already state ("Fasting glucose (mg/dl)", "Creatinine (mg/dl)").
BODY_WEIGHT = _Concept(LOINC_SYSTEM, "29463-7", "Body weight", "kg",
                       ObservationCategory.VITAL_SIGNS)
BODY_HEIGHT = _Concept(LOINC_SYSTEM, "8302-2", "Body height", "cm",
                       ObservationCategory.VITAL_SIGNS)
FASTING_GLUCOSE = _Concept(LOINC_SYSTEM, "1558-6",
                           "Fasting glucose [Mass/volume] in Serum or Plasma", "mg/dL",
                           ObservationCategory.LABORATORY)
CREATININE = _Concept(LOINC_SYSTEM, "2160-0",
                      "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL",
                      ObservationCategory.LABORATORY)

# Units accepted in a free-text lab string, normalised to UCUM. A unit that is not
# recognised is NOT coerced to the default — the value is kept as text instead, because
# silently relabelling mmol/L as mg/dL changes the number's meaning by a factor of ~18.
_UNIT_ALIASES: dict[str, str] = {
    "mg/dl": "mg/dL", "mgdl": "mg/dL", "mg%": "mg/dL",
    "mmol/l": "mmol/L",
    "umol/l": "umol/L", "µmol/l": "umol/L",
    "g/dl": "g/dL",
    "kg": "kg", "cm": "cm", "m": "m",
}

_QUANTITY_RE = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(.*)$")


def _loaded_value(obj: Any, name: str) -> Any:
    """Read an ORM attribute *without* triggering a lazy refresh.

    Columns with a ``server_default`` (``created_at``, ``updated_at``) are expired
    immediately after a flush, so reading them through ordinary attribute access issues a
    SELECT. On an ``AsyncSession`` that implicit IO raises ``MissingGreenlet`` — and,
    worse, leaves the session unusable, so the *caller* fails too even when the exception
    is caught here. Reading ``__dict__`` returns the value only if it is already loaded
    and never performs IO, so callers fall back to a supplied default instead.
    """
    try:
        return obj.__dict__.get(name)
    except AttributeError:
        return None


def parse_quantity(
    raw: Any, default_unit: str | None
) -> tuple[float | None, str | None, str | None]:
    """Parse a free-text clinical value into ``(quantity, unit, leftover_text)``.

    Returns a quantity only when the string is unambiguously a number optionally followed
    by a *recognised* unit. Anything else ("normal", "<0.5", "110 foo") comes back as
    leftover text for ``value_string``, because a guessed number or a guessed unit is a
    fabricated measurement.

        >>> parse_quantity("5.4", "mg/dL")
        (5.4, 'mg/dL', None)
        >>> parse_quantity("96 mmol/L", "mg/dL")      # stated unit wins over the default
        (96.0, 'mmol/L', None)
        >>> parse_quantity("normal", "mg/dL")
        (None, None, 'normal')
        >>> parse_quantity("<0.5", "mg/dL")
        (None, None, '<0.5')
    """
    if raw is None:
        return None, None, None
    text = str(raw).strip()
    if not text:
        return None, None, None

    match = _QUANTITY_RE.match(text)
    if not match:
        return None, None, text

    number, remainder = match.group(1), match.group(2).strip()
    try:
        value = float(number.replace(",", "."))
    except ValueError:
        return None, None, text

    if not remainder:
        return value, default_unit, None

    unit = _UNIT_ALIASES.get(remainder.lower())
    if unit is None:
        # A trailing token we do not recognise: could be a unit we would mis-assign, a
        # qualifier ("110 high"), or a range. Keep the whole thing as text.
        logger.warning("observation_unit_unrecognised", value=text[:64])
        return None, None, text
    return value, unit, None


# ── Mammography slots ─────────────────────────────────────────────────────────
# Local codes: BI-RADS / density LOINC bindings await clinical sign-off (step 5).
# SNOMED 76752008 "Breast structure" is the bodySite for all of them.
_BREAST_STRUCTURE_SNOMED = "76752008"

_MAMMO_SLOTS: dict[str, str] = {
    "density": "Breast composition (BI-RADS density)",
    "mass": "Mass",
    "calcification": "Calcification",
    "skin_thickening": "Skin thickening",
    "nipple_retraction": "Nipple retraction",
    "architectural_distortion": "Architectural distortion",
    "axillary_nodes": "Axillary lymph nodes",
}
_BIRADS_DISPLAY = "BI-RADS assessment category"


class ObservationService:
    """Derives Observations from the platform's clinical records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Source 1: intake orders ───────────────────────────────────────────────

    async def record_order_observations(
        self,
        order: Any,
        patient_ref: str,
        tenant_id: str = "default",
    ) -> int:
        """(Re)derive the vitals/labs Observations for one intake order.

        ``effective_dt`` prefers an explicitly captured draw/measurement time and falls
        back to the order's creation time. That fallback is an approximation and is worth
        naming: a glucose drawn three days before the scan means something different from
        one drawn at injection, which is why the API accepts the real timestamps.
        """
        source = f"order:{getattr(order, 'id', '')}"
        if not patient_ref or not getattr(order, "id", None):
            return 0

        # Never plain-getattr a server-default column here — see _loaded_value.
        authored = _loaded_value(order, "created_at") or utcnow()
        rows: list[ObservationRecord] = []

        def _add(concept: _Concept, raw_value: Any, effective: datetime | None) -> None:
            quantity, unit, leftover = parse_quantity(raw_value, concept.unit)
            if quantity is None and not leftover:
                return
            rows.append(
                self._build(
                    tenant_id=tenant_id,
                    patient_ref=patient_ref,
                    patient_id=getattr(order, "patient_id", None),
                    # Deliberately NOT attached to the study, even when the order is.
                    # observations.study_instance_uid is ON DELETE CASCADE, which is right
                    # for AI measurements (purging a study should purge what was derived
                    # from it) but wrong here: a weight or a fasting glucose is a fact
                    # about the patient at a point in time, not about one imaging study.
                    # Deleting a study — a real operation, both via DELETE /studies and
                    # via a retention policy — must not erase the patient's lab history.
                    # The study association is still recoverable from
                    # orders.study_instance_uid, and clinically the useful query is
                    # "the glucose nearest this study's date", which patient_ref +
                    # effective_dt already answers.
                    study_instance_uid=None,
                    concept=concept,
                    value_quantity=quantity,
                    value_unit=unit if quantity is not None else None,
                    value_string=leftover,
                    effective_dt=effective or authored,
                    source=source,
                )
            )

        _add(BODY_WEIGHT, getattr(order, "weight_kg", None), authored)
        _add(BODY_HEIGHT, getattr(order, "height_cm", None), authored)
        _add(
            FASTING_GLUCOSE,
            getattr(order, "fasting_glucose", None),
            getattr(order, "fasting_glucose_dt", None),
        )
        _add(
            CREATININE,
            getattr(order, "creatinine", None),
            getattr(order, "creatinine_dt", None),
        )

        return await self._replace(source, rows)

    # ── Source 3: mammography report slots ────────────────────────────────────

    async def record_mammography_observations(
        self,
        report: Any,
        patient_ref: str,
        tenant_id: str = "default",
    ) -> int:
        """Pivot the per-breast report slots into rows carrying explicit laterality.

        The slots are structured and CHECK-constrained already; what they lacked was an
        addressable form — ``mass_right`` puts the side in the column *name*, so no query
        can ask "all right-breast mass findings". Status follows the report: a radiologist
        report row exists only once authored, so these are ``final``.
        """
        study_uid = getattr(report, "study_instance_uid", None)
        source = f"mammography:{study_uid}"
        if not patient_ref or not study_uid:
            return 0

        # utcnow() is the correct fallback, not merely a safe one: the report row is
        # written at save time, so "now" IS when the radiologist authored these findings.
        effective = (
            _loaded_value(report, "updated_at")
            or _loaded_value(report, "created_at")
            or utcnow()
        )
        rows: list[ObservationRecord] = []

        for side in ("right", "left"):
            laterality = Laterality.RIGHT if side == "right" else Laterality.LEFT

            for slot, display in _MAMMO_SLOTS.items():
                value = getattr(report, f"{slot}_{side}", None)
                if value in (None, ""):
                    continue
                rows.append(
                    self._build(
                        tenant_id=tenant_id,
                        patient_ref=patient_ref,
                        patient_id=None,
                        study_instance_uid=study_uid,
                        concept=_Concept(
                            LOCAL_SYSTEM, f"mammo.{slot}", display, None,
                            ObservationCategory.IMAGING,
                        ),
                        value_codeable_code=str(value),
                        value_codeable_system=LOCAL_SYSTEM,
                        body_site_code=_BREAST_STRUCTURE_SNOMED,
                        laterality=laterality,
                        effective_dt=effective,
                        source=source,
                    )
                )

            birads = getattr(report, f"birads_{side}", None)
            if birads not in (None, ""):
                rows.append(
                    self._build(
                        tenant_id=tenant_id,
                        patient_ref=patient_ref,
                        patient_id=None,
                        study_instance_uid=study_uid,
                        concept=_Concept(
                            LOCAL_SYSTEM, "mammo.birads", _BIRADS_DISPLAY, None,
                            ObservationCategory.IMAGING,
                        ),
                        value_codeable_code=str(birads),
                        value_codeable_system=LOCAL_SYSTEM,
                        body_site_code=_BREAST_STRUCTURE_SNOMED,
                        laterality=laterality,
                        effective_dt=effective,
                        source=source,
                    )
                )

        return await self._replace(source, rows)

    # ── Source 2: AI results, via the per-plugin whitelist ────────────────────

    async def record_result_observations(
        self,
        usecase_name: str,
        result: dict[str, Any],
        patient_ref: str,
        study_instance_uid: str,
        result_id: str | None = None,
        model_version: str | None = None,
        model_checksum: str | None = None,
        effective_dt: datetime | None = None,
        tenant_id: str = "default",
    ) -> int:
        """Derive Observations from a pipeline result, strictly per the plugin's map.

        A plugin with no ``fhir_map.yaml`` yields nothing — that is the safe default and
        it is what every narrative-only plugin relies on. Values are coded with the
        plugin's LOCAL codes; a standard (LOINC/SNOMED) code is attached only where the
        map marks it verified, so an unsigned-off code can never reach a chart.

        Status is ``preliminary``: these are algorithm outputs, not findings a
        radiologist has verified. They become part of a ``final`` report only once the
        study is signed, which is a separate assertion.
        """
        from app.application.fhir_terminology import extract_values

        source = f"result:{usecase_name}:{study_instance_uid}"
        if not patient_ref or not study_instance_uid:
            return 0

        extracted = extract_values(usecase_name, result)
        if not extracted:
            return 0

        device = None
        if model_version:
            device = f"{model_version}:{model_checksum}" if model_checksum else model_version

        rows: list[ObservationRecord] = []
        skipped_no_unit = 0
        for item in extracted:
            spec = item.spec
            quantity: float | None = None
            codeable: str | None = None
            text: str | None = None

            if spec.value_codes or isinstance(item.value, bool):
                # Booleans first: bool is a subclass of int, so an unguarded numeric
                # branch would turn amyloid_positive=True into the quantity 1.
                codeable = str(item.value).lower() if isinstance(item.value, bool) \
                    else str(item.value)
            elif isinstance(item.value, (int, float)):
                if not spec.unit:
                    # Refuse rather than emit a unitless quantity (the old exporter's
                    # `unit: "unknown"` failure). A map entry for a numeric value must
                    # declare a UCUM unit.
                    skipped_no_unit += 1
                    logger.warning(
                        "observation_spec_missing_unit",
                        usecase=usecase_name, code=spec.code, path=spec.path,
                    )
                    continue
                quantity = float(item.value)
            else:
                text = str(item.value)[:512]

            concept = _Concept(
                LOCAL_SYSTEM, spec.code, item.display, spec.unit, spec.category
            )
            rows.append(
                self._build(
                    tenant_id=tenant_id,
                    patient_ref=patient_ref,
                    patient_id=None,
                    study_instance_uid=study_instance_uid,
                    concept=concept,
                    value_quantity=quantity,
                    value_unit=spec.unit if quantity is not None else None,
                    value_string=text,
                    value_codeable_code=codeable,
                    value_codeable_system=LOCAL_SYSTEM if codeable else None,
                    body_site_code=spec.body_site_code,
                    effective_dt=effective_dt,
                    result_id=result_id,
                    derived_from_device=device,
                    status=ObservationStatus.PRELIMINARY,
                    source=source,
                )
            )

        if skipped_no_unit:
            logger.warning(
                "observation_specs_skipped",
                usecase=usecase_name, count=skipped_no_unit, reason="numeric value without unit",
            )
        return await self._replace(source, rows)

    # ── Read side (the FHIR search axes) ──────────────────────────────────────

    async def search(
        self,
        patient_ref: str | None = None,
        code: str | None = None,
        category: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        study_instance_uid: str | None = None,
        tenant_id: str = "default",
        limit: int = 200,
    ) -> list[ObservationRecord]:
        """The four canonical Observation search axes (patient, code, category, date).

        Ordered so the ``ix_observations_search`` composite index is usable: tenant and
        patient_ref first, then code, then the date range.
        """
        stmt = select(ObservationRecord).where(ObservationRecord.tenant_id == tenant_id)
        if patient_ref:
            stmt = stmt.where(ObservationRecord.patient_ref == patient_ref)
        if code:
            stmt = stmt.where(ObservationRecord.code == code)
        if category:
            stmt = stmt.where(ObservationRecord.category == category)
        if study_instance_uid:
            stmt = stmt.where(ObservationRecord.study_instance_uid == study_instance_uid)
        if date_from:
            stmt = stmt.where(ObservationRecord.effective_dt >= date_from)
        if date_to:
            stmt = stmt.where(ObservationRecord.effective_dt <= date_to)
        stmt = stmt.order_by(ObservationRecord.effective_dt.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build(
        self,
        *,
        tenant_id: str,
        patient_ref: str,
        patient_id: str | None,
        study_instance_uid: str | None,
        concept: _Concept,
        source: str,
        value_quantity: float | None = None,
        value_unit: str | None = None,
        value_string: str | None = None,
        value_codeable_code: str | None = None,
        value_codeable_system: str | None = None,
        body_site_code: str | None = None,
        laterality: Laterality | None = None,
        effective_dt: datetime | None = None,
        result_id: str | None = None,
        derived_from_device: str | None = None,
        status: ObservationStatus = ObservationStatus.FINAL,
    ) -> ObservationRecord:
        if value_quantity is not None and not value_unit:
            # Mirrors the DB CHECK; failing here names the concept, which the constraint
            # violation would not.
            raise ValueError(
                f"refusing Observation {concept.code}: a quantity requires a UCUM unit"
            )
        return ObservationRecord(
            tenant_id=tenant_id or "default",
            patient_ref=patient_ref,
            patient_id=patient_id,
            study_instance_uid=study_instance_uid,
            result_id=result_id,
            category=concept.category.value,
            code_system=concept.system,
            code=concept.code,
            code_display=concept.display,
            value_quantity=value_quantity,
            value_unit=value_unit,
            value_string=value_string,
            value_codeable_code=value_codeable_code,
            value_codeable_system=value_codeable_system,
            body_site_code=body_site_code,
            laterality=laterality.value if laterality else None,
            status=status.value,
            effective_dt=effective_dt,
            issued=utcnow(),
            derived_from_device=derived_from_device,
            source=source,
        )

    async def _replace(self, source: str, rows: list[ObservationRecord]) -> int:
        """Delete this source's previous rows, then insert the current set.

        Scoped strictly to ``source`` so one writer can never remove another's rows.
        Replacement rather than append because these are *derived* values: a report slot
        edited twice must not leave two contradictory findings for the same study.
        """
        await self._session.execute(
            delete(ObservationRecord).where(ObservationRecord.source == source)
        )
        for row in rows:
            self._session.add(row)
        await self._session.flush()
        logger.info("observations_recorded", source=source, count=len(rows))
        return len(rows)
