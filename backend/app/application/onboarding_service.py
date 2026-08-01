"""Patient onboarding service: de-identified patient + order intake.

POST /orders is one transaction: upsert the patient by (tenant, patient_ref),
create the order, optionally link an existing study (by study_instance_uid, or
auto-match by patient_ref == studies.patient_id), and audit. Validation per the
spec (consent required, enum constraints). The router commits once at the end, so
any failure rolls back the whole unit (no orphan patient/order).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import desc, func, select

from app.domain.enums import AuditActorType, audit_action_to_crude

logger = structlog.get_logger(__name__)


def _audit_actor_fields(actor: str | None) -> dict[str, str | None]:
    """Type a legacy free-text ``actor`` for the AuditEvent columns.

    Callers historically passed a user id, a username, or a machine name. A value that is
    neither a known machine actor nor plainly an id is still kept in ``actor_display`` but
    leaves ``actor_id`` NULL — an unattributed entry beats a misattributed one.
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

SEX_VALUES = {"female", "male", "other"}
PRIORITIES = {"routine", "stat"}


def _normalize_age(raw: Any) -> str:
    """Validate an intake age as a whole number of years (0-150), returned as a string.

    Stored on ``PatientRecord.age_band`` (column kept for back-compat; it now holds an
    exact age rather than a coarse band). Raises OnboardingValidationError if invalid.
    """
    s = str(raw or "").strip()
    if not s.isdigit():
        raise OnboardingValidationError("age must be a whole number of years")
    n = int(s)
    if not (0 <= n <= 150):
        raise OnboardingValidationError("age must be between 0 and 150 years")
    return str(n)


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _same_tenant(tenant_id: str):
    """Tenant predicate for StudyRecord.

    ``studies.tenant_id`` is nullable with a server default, so rows written before
    multi-tenancy hold NULL — COALESCE makes those belong to "default" rather than
    matching nothing. Mirrors migration 028's backfill.
    """
    from app.infrastructure.database.models import StudyRecord

    return func.coalesce(StudyRecord.tenant_id, "default") == (tenant_id or "default")


def _to_datetime(v) -> datetime | None:
    """Accept an ISO string or a datetime from the API layer; None for anything else.

    Naive input is treated as UTC — every timestamp column is timestamptz (migration
    027) and asyncpg will not bind a naive value to one."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            logger.warning("order_timestamp_unparseable", value=str(v)[:64])
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bmi(height_cm, weight_kg) -> float | None:
    """Body Mass Index (kg/m²), derived from height/weight. None if either is missing
    or non-positive. Rounded to one decimal."""
    h = _to_float(height_cm)
    w = _to_float(weight_kg)
    if not h or not w or h <= 0 or w <= 0:
        return None
    return round(w / ((h / 100.0) ** 2), 1)


class OnboardingValidationError(ValueError):
    """Validation failure (→ 422)."""


class OnboardingService:
    def __init__(self, session):
        self._session = session

    # ── Patient search ─────────────────────────────────────────────────────────

    async def list_patients(self, query: str = "", tenant_id: str = "default") -> list[dict[str, Any]]:
        from app.infrastructure.database.models import PatientRecord

        stmt = select(PatientRecord).where(PatientRecord.tenant_id == tenant_id)
        if query:
            stmt = stmt.where(PatientRecord.patient_ref.ilike(f"%{query}%"))
        stmt = stmt.order_by(desc(PatientRecord.created_at)).limit(100)
        res = await self._session.execute(stmt)
        return [self._patient_dict(p) for p in res.scalars().all()]

    async def get_patient(self, patient_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
        """Return a patient + their orders (for the edit view)."""
        from app.infrastructure.database.models import OrderRecord, PatientRecord

        patient = (await self._session.execute(
            select(PatientRecord).where(
                PatientRecord.id == patient_id, PatientRecord.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if not patient:
            return None
        orders = (await self._session.execute(
            select(OrderRecord).where(OrderRecord.patient_id == patient_id)
            .order_by(desc(OrderRecord.created_at))
        )).scalars().all()

        # Merge each order's coded diagnosis back in. It lives in `conditions`, not on the
        # order, so without this the edit form would silently blank a previously entered
        # code — and the next save would then delete the Condition.
        order_dicts = [self._order_dict(o) for o in orders]
        if order_dicts:
            from app.infrastructure.database.models import ConditionRecord

            rows = (await self._session.execute(
                select(ConditionRecord).where(
                    ConditionRecord.order_id.in_([o["id"] for o in order_dicts])
                )
            )).scalars().all()
            by_order = {r.order_id: r for r in rows}
            for od in order_dicts:
                cond = by_order.get(od["id"])
                od["diagnosis_system"] = cond.code_system if cond else None
                od["diagnosis_code"] = cond.code if cond else None
                od["diagnosis_display"] = cond.code_display if cond else None

        return {
            "patient": self._patient_dict(patient),
            "orders": order_dicts,
        }

    # ── Edits ───────────────────────────────────────────────────────────────────

    async def update_patient(
        self, patient_id: str, payload: dict[str, Any], actor_id: str | None, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import PatientRecord

        patient = (await self._session.execute(
            select(PatientRecord).where(
                PatientRecord.id == patient_id, PatientRecord.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if not patient:
            return None
        before = {"sex": patient.sex, "age_band": patient.age_band}
        if "sex" in payload and payload["sex"] is not None:
            sex = str(payload["sex"]).strip().lower()
            if sex not in SEX_VALUES:
                raise OnboardingValidationError(f"sex must be one of {sorted(SEX_VALUES)}")
            patient.sex = sex
        if "age_band" in payload and payload["age_band"] is not None:
            patient.age_band = _normalize_age(payload["age_band"])
        await self._session.flush()
        await self._audit(actor_id, "patient_updated", patient.id,
                          {"before": before, "after": {"sex": patient.sex, "age_band": patient.age_band}})
        return self._patient_dict(patient)

    async def update_order(
        self, order_id: str, payload: dict[str, Any], actor_id: str | None, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import (
            OrderRecord,
            PatientRecord,
            StudyRecord,
        )

        order = (await self._session.execute(
            select(OrderRecord).where(OrderRecord.id == order_id, OrderRecord.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if not order:
            return None

        # Text fields — if provided, must be non-empty.
        for field in ("modality", "indication", "region_profile"):
            if field in payload and payload[field] is not None:
                val = str(payload[field]).strip()
                if not val:
                    raise OnboardingValidationError(f"{field} cannot be empty")
                setattr(order, field, val)
        # body_part is optional (the study/modality type captures it).
        if "body_part" in payload:
            order.body_part = (str(payload["body_part"]).strip() or None) if payload["body_part"] is not None else None
        if "referrer" in payload:
            order.referrer = (str(payload["referrer"]).strip() or None) if payload["referrer"] is not None else None
        # Optional free-text / numeric clinical fields.
        for field in (
            "clinical_history", "comparative_study", "fasting_glucose",
            "injection_site", "creatinine", "external_order_ref",
        ):
            if field in payload:
                setattr(order, field, (str(payload[field]).strip() or None) if payload[field] is not None else None)
        for field in ("height_cm", "weight_kg"):
            if field in payload:
                setattr(order, field, _to_float(payload[field]))
        for field in ("fasting_glucose_dt", "creatinine_dt"):
            if field in payload:
                setattr(order, field, _to_datetime(payload[field]))
        if "priority" in payload and payload["priority"] is not None:
            priority = str(payload["priority"]).strip().lower()
            if priority not in PRIORITIES:
                raise OnboardingValidationError(f"priority must be one of {sorted(PRIORITIES)}")
            order.priority = priority
        if "consent_ack" in payload and payload["consent_ack"] is not None:
            order.consent_ack = bool(payload["consent_ack"])
        if "study_instance_uid" in payload:
            uid = (str(payload["study_instance_uid"]).strip() or None) if payload["study_instance_uid"] is not None else None
            if uid:
                exists = (await self._session.execute(
                    select(StudyRecord.study_instance_uid).where(
                        StudyRecord.study_instance_uid == uid, _same_tenant(tenant_id)
                    )
                )).scalar_one_or_none()
                if not exists:
                    raise OnboardingValidationError("study_instance_uid not found")
            order.study_instance_uid = uid

        await self._session.flush()
        await self._link_study_to_patient(
            order.study_instance_uid, order.patient_id, order.tenant_id or tenant_id
        )
        # Re-derive this order's Observations from the edited values.
        patient_ref = (await self._session.execute(
            select(PatientRecord.patient_ref).where(PatientRecord.id == order.patient_id)
        )).scalar_one_or_none()
        # Payload built before the hook — see create_order for why.
        response = self._order_dict(order)
        await self._record_observations(order, patient_ref)
        await self._record_condition(order, patient_ref, payload)
        await self._audit(actor_id, "order_updated", order.id, response)
        return response

    # ── Order intake (single transaction) ──────────────────────────────────────

    async def create_order(
        self, payload: dict[str, Any], actor_id: str | None, tenant_id: str = "default"
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import OrderRecord, PatientRecord, StudyRecord

        patient_ref = (payload.get("patient_ref") or "").strip()
        sex = (payload.get("sex") or "").strip().lower()
        age_band = (payload.get("age_band") or "").strip()
        modality = (payload.get("modality") or "").strip()
        body_part = (payload.get("body_part") or "").strip() or None
        indication = (payload.get("indication") or "").strip()
        region_profile = (payload.get("region_profile") or "").strip()
        referrer = (payload.get("referrer") or "").strip() or None
        priority = (payload.get("priority") or "routine").strip().lower()
        consent_ack = bool(payload.get("consent_ack", False))
        study_instance_uid = (payload.get("study_instance_uid") or "").strip() or None
        clinical_history = (payload.get("clinical_history") or "").strip() or None
        comparative_study = (payload.get("comparative_study") or "").strip() or None
        fasting_glucose = (payload.get("fasting_glucose") or "").strip() or None
        injection_site = (payload.get("injection_site") or "").strip() or None
        creatinine = (str(payload.get("creatinine") or "")).strip() or None
        external_order_ref = (payload.get("external_order_ref") or "").strip() or None
        fasting_glucose_dt = _to_datetime(payload.get("fasting_glucose_dt"))
        creatinine_dt = _to_datetime(payload.get("creatinine_dt"))
        height_cm = _to_float(payload.get("height_cm"))
        weight_kg = _to_float(payload.get("weight_kg"))

        # ── Validation (spec §4) ──────────────────────────────────────────────
        if not patient_ref:
            raise OnboardingValidationError("patient_ref is required")
        if sex not in SEX_VALUES:
            raise OnboardingValidationError(f"sex must be one of {sorted(SEX_VALUES)}")
        age_band = _normalize_age(age_band)
        if not modality:
            raise OnboardingValidationError("modality is required")
        if not indication:
            raise OnboardingValidationError("indication is required")
        if not region_profile:
            raise OnboardingValidationError("region_profile is required")
        if priority not in PRIORITIES:
            raise OnboardingValidationError(f"priority must be one of {sorted(PRIORITIES)}")
        if not consent_ack:
            raise OnboardingValidationError("consent acknowledgment required")

        # ── Upsert patient by (tenant, patient_ref) ───────────────────────────
        existing = (await self._session.execute(
            select(PatientRecord).where(
                PatientRecord.tenant_id == tenant_id,
                PatientRecord.patient_ref == patient_ref,
            )
        )).scalar_one_or_none()
        patient_is_new = existing is None
        if existing:
            patient = existing
            patient.sex = sex
            patient.age_band = age_band
        else:
            patient = PatientRecord(
                id=str(uuid.uuid4()), patient_ref=patient_ref,
                sex=sex, age_band=age_band, tenant_id=tenant_id,
            )
            self._session.add(patient)
        await self._session.flush()

        # ── Link study: explicit UID, else auto-match by patient_ref == MRN ────
        linked_uid = None
        if study_instance_uid:
            exists = (await self._session.execute(
                select(StudyRecord.study_instance_uid).where(
                    StudyRecord.study_instance_uid == study_instance_uid,
                    _same_tenant(tenant_id),
                )
            )).scalar_one_or_none()
            if not exists:
                raise OnboardingValidationError("study_instance_uid not found")
            linked_uid = study_instance_uid
        else:
            # Auto-link if exactly one study in THIS TENANT matches this MRN.
            # The tenant filter is not optional: MRNs are only unique within the
            # issuing hospital, so without it a colliding MRN from another tenant
            # auto-links this order to a different hospital's study — and then
            # _link_study_to_patient would write a durable wrong-patient reference,
            # which is what every exported resource's subject derives from.
            matches = (await self._session.execute(
                select(StudyRecord.study_instance_uid).where(
                    StudyRecord.patient_id == patient_ref,
                    _same_tenant(tenant_id),
                )
            )).scalars().all()
            if len(matches) == 1:
                linked_uid = matches[0]

        order = OrderRecord(
            id=str(uuid.uuid4()),
            patient_id=patient.id,
            modality=modality, body_part=body_part, referrer=referrer,
            priority=priority, indication=indication, region_profile=region_profile,
            consent_ack=consent_ack, study_instance_uid=linked_uid,
            clinical_history=clinical_history, comparative_study=comparative_study,
            height_cm=height_cm, weight_kg=weight_kg,
            fasting_glucose=fasting_glucose, injection_site=injection_site,
            creatinine=creatinine, external_order_ref=external_order_ref,
            fasting_glucose_dt=fasting_glucose_dt, creatinine_dt=creatinine_dt,
            created_by=actor_id or None, tenant_id=tenant_id,
        )
        self._session.add(order)
        await self._session.flush()

        # Establish the referential study→patient link alongside the MRN association.
        await self._link_study_to_patient(linked_uid, patient.id, tenant_id)

        # Materialise the response before the derived-data hook — the hook writes through
        # this same session, and reading ORM attributes afterwards can trigger implicit IO
        # (MissingGreenlet on an AsyncSession). See MammographyService.upsert_report.
        response = {"order": self._order_dict(order), "patient": self._patient_dict(patient)}
        await self._record_observations(order, patient_ref)
        await self._record_condition(order, patient_ref, payload)

        if patient_is_new:
            await self._audit(actor_id, "patient_created", patient.id, {"patient_ref": patient_ref})
        await self._audit(
            actor_id, "order_created", order.id,
            {"patient_ref": patient_ref, "modality": modality, "study": linked_uid},
        )
        return response

    async def link_study(
        self, order_id: str, study_uid: str, actor_id: str | None, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import OrderRecord, StudyRecord

        order = (await self._session.execute(
            select(OrderRecord).where(OrderRecord.id == order_id, OrderRecord.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if not order:
            return None
        exists = (await self._session.execute(
            select(StudyRecord.study_instance_uid).where(
                StudyRecord.study_instance_uid == study_uid, _same_tenant(tenant_id)
            )
        )).scalar_one_or_none()
        if not exists:
            raise OnboardingValidationError("study_instance_uid not found")
        order.study_instance_uid = study_uid
        await self._session.flush()
        await self._link_study_to_patient(
            study_uid, order.patient_id, order.tenant_id or tenant_id
        )
        await self._audit(actor_id, "order_linked_study", order.id, {"study": study_uid})
        return self._order_dict(order)

    # ── Clinical lookup for reports ─────────────────────────────────────────────

    async def get_clinical_for_study(self, study_uid: str) -> dict[str, Any] | None:
        """Return clinical intake fields for a study (direct link first, then MRN
        match). Used to populate the report. None if no order is found."""
        from app.infrastructure.database.models import OrderRecord, PatientRecord, StudyRecord

        order = (await self._session.execute(
            select(OrderRecord).where(OrderRecord.study_instance_uid == study_uid)
            .order_by(desc(OrderRecord.created_at))
        )).scalars().first()

        if order is None:
            study = (await self._session.execute(
                select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
            )).scalar_one_or_none()
            if study and study.patient_id:
                order = (await self._session.execute(
                    select(OrderRecord)
                    .join(PatientRecord, OrderRecord.patient_id == PatientRecord.id)
                    .where(PatientRecord.patient_ref == study.patient_id)
                    .order_by(desc(OrderRecord.created_at))
                )).scalars().first()

        if order is None:
            return None
        patient = (await self._session.execute(
            select(PatientRecord).where(PatientRecord.id == order.patient_id)
        )).scalar_one_or_none()
        return {
            "indication": order.indication,
            "clinical_history": order.clinical_history or order.indication,
            "comparative_study": order.comparative_study,
            "referrer": order.referrer,
            "priority": order.priority,
            "region_profile": order.region_profile,
            "consent_ack": order.consent_ack,
            "modality": order.modality,
            "body_part": order.body_part,
            "height_cm": order.height_cm,
            "weight_kg": order.weight_kg,
            "bmi": _bmi(order.height_cm, order.weight_kg),
            "fasting_glucose": order.fasting_glucose,
            "injection_site": order.injection_site,
            "creatinine": order.creatinine,
            "sex": patient.sex if patient else None,
            "age_band": patient.age_band if patient else None,
            "patient_ref": patient.patient_ref if patient else None,
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _record_observations(self, order: Any, patient_ref: str | None) -> None:
        """Derive the vitals/labs Observations for an order (source 1 of step 4).

        Best-effort by design: a derived clinical row must never be able to fail an
        intake submission. Idempotent — ObservationService replaces this order's own
        rows, so update_order re-deriving is correct rather than duplicative.
        """
        if not patient_ref:
            return
        from app.config import get_settings

        if not get_settings().observations_enabled:
            return
        try:
            from app.application.observation_service import ObservationService

            # SAVEPOINT so a failure here rolls back only the derived rows. This hook
            # shares the intake transaction; without the savepoint a failure could leave
            # the session unusable and fail the whole order submission, which catching the
            # exception alone does not prevent.
            async with self._session.begin_nested():
                await ObservationService(self._session).record_order_observations(
                    order, patient_ref=patient_ref, tenant_id=order.tenant_id or "default"
                )
        except Exception as exc:
            logger.warning(
                "order_observations_failed", order_id=getattr(order, "id", None), error=str(exc)
            )

    async def _record_condition(
        self, order: Any, patient_ref: str | None, payload: dict[str, Any]
    ) -> None:
        """Derive the coded Condition for an order (step 6).

        Unlike the Observation hook this one lets a *validation* error surface: a bad
        diagnosis code is the caller's mistake and they should be told (422), not have it
        silently dropped. Any other failure is swallowed inside a SAVEPOINT, on the same
        reasoning as the Observation hook — derived data must not fail an intake.
        """
        if not patient_ref:
            return
        # Always call through, even with no code supplied: clearing the code on an edit
        # must remove the previously derived row, which record_order_condition handles.
        from app.application.condition_service import (
            ConditionService,
            ConditionValidationError,
        )

        try:
            async with self._session.begin_nested():
                await ConditionService(self._session).record_order_condition(
                    order,
                    patient_ref=patient_ref,
                    code_system=payload.get("diagnosis_system"),
                    code=payload.get("diagnosis_code"),
                    code_display=payload.get("diagnosis_display"),
                    onset_dt=_to_datetime(payload.get("diagnosis_onset_dt")),
                    tenant_id=order.tenant_id or "default",
                )
        except ConditionValidationError as exc:
            raise OnboardingValidationError(str(exc)) from exc
        except Exception as exc:
            logger.warning(
                "order_condition_failed", order_id=getattr(order, "id", None), error=str(exc)
            )

    async def _link_study_to_patient(
        self, study_uid: str | None, patient_id: str | None, tenant_id: str = "default"
    ) -> bool:
        """Set ``studies.patient_record_id`` — the referential link behind every
        exported resource's ``subject``.

        Called from each path that associates a study with an order, so the reference
        is established at the same moment as the MRN-based association rather than
        being inferred by a string join at read time (see get_clinical_for_study).

        The tenant gate is repeated here rather than trusted from the caller: this
        writes the single field that decides *which patient* an exported report is
        attached to, so a cross-tenant write is the worst outcome the platform can
        produce and is worth refusing twice. A refusal is logged at warning level
        because it means a caller tried.

        Overwrites an existing link only when it actually differs, and logs that case:
        a study changing patients is either a correction or a mistake, and both are
        worth being able to find afterwards. Returns True if the row was changed.
        """
        from app.infrastructure.database.models import StudyRecord

        if not study_uid or not patient_id:
            return False

        study = (await self._session.execute(
            select(StudyRecord).where(
                StudyRecord.study_instance_uid == study_uid, _same_tenant(tenant_id)
            )
        )).scalar_one_or_none()
        if study is None:
            # Either the study does not exist, or it belongs to another tenant. Both are
            # refusals; distinguish them in the log so a real misconfiguration is visible.
            other = (await self._session.execute(
                select(StudyRecord.tenant_id).where(
                    StudyRecord.study_instance_uid == study_uid
                )
            )).scalar_one_or_none()
            if other is not None:
                logger.warning(
                    "study_patient_link_cross_tenant_refused",
                    study_uid=study_uid,
                    study_tenant=other,
                    requested_tenant=tenant_id,
                )
            return False
        if study.patient_record_id == patient_id:
            return False

        if study.patient_record_id:
            logger.warning(
                "study_patient_link_reassigned",
                study_uid=study_uid,
                previous_patient_id=study.patient_record_id,
                new_patient_id=patient_id,
            )
        study.patient_record_id = patient_id
        await self._session.flush()
        return True

    async def _audit(self, actor: str | None, action: str, entity_id: str, details: dict) -> None:
        from app.infrastructure.database.models import AuditLogRecord

        self._session.add(AuditLogRecord(
            id=str(uuid.uuid4()),
            action=action,
            entity_type="onboarding",
            entity_id=entity_id,
            **_audit_actor_fields(actor),
            action_crude=audit_action_to_crude(action),
            details=details,
        ))

    @staticmethod
    def _patient_dict(p) -> dict[str, Any]:
        return {
            "id": p.id, "patient_ref": p.patient_ref, "sex": p.sex,
            "age_band": p.age_band, "tenant_id": p.tenant_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }

    @staticmethod
    def _order_dict(o) -> dict[str, Any]:
        return {
            "id": o.id, "patient_id": o.patient_id, "modality": o.modality,
            "body_part": o.body_part, "referrer": o.referrer, "priority": o.priority,
            "indication": o.indication, "region_profile": o.region_profile,
            "consent_ack": o.consent_ack, "study_instance_uid": o.study_instance_uid,
            "clinical_history": o.clinical_history, "comparative_study": o.comparative_study,
            "height_cm": o.height_cm, "weight_kg": o.weight_kg,
            "bmi": _bmi(o.height_cm, o.weight_kg),
            "fasting_glucose": o.fasting_glucose, "injection_site": o.injection_site,
            "creatinine": o.creatinine,
            "external_order_ref": getattr(o, "external_order_ref", None),
            "fasting_glucose_dt": (
                o.fasting_glucose_dt.isoformat() if getattr(o, "fasting_glucose_dt", None) else None
            ),
            "creatinine_dt": (
                o.creatinine_dt.isoformat() if getattr(o, "creatinine_dt", None) else None
            ),
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
