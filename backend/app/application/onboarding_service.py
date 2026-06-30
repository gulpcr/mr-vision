"""Patient onboarding service: de-identified patient + order intake.

POST /orders is one transaction: upsert the patient by (tenant, patient_ref),
create the order, optionally link an existing study (by study_instance_uid, or
auto-match by patient_ref == studies.patient_id), and audit. Validation per the
spec (consent required, enum constraints). The router commits once at the end, so
any failure rolls back the whole unit (no orphan patient/order).
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import desc, func, select

logger = structlog.get_logger(__name__)

SEX_VALUES = {"female", "male", "other"}
AGE_BANDS = {"0-17", "18-39", "40-64", "65+"}
PRIORITIES = {"routine", "stat"}


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
        return {
            "patient": self._patient_dict(patient),
            "orders": [self._order_dict(o) for o in orders],
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
            age_band = str(payload["age_band"]).strip()
            if age_band not in AGE_BANDS:
                raise OnboardingValidationError(f"age_band must be one of {sorted(AGE_BANDS)}")
            patient.age_band = age_band
        await self._session.flush()
        await self._audit(actor_id, "patient_updated", patient.id,
                          {"before": before, "after": {"sex": patient.sex, "age_band": patient.age_band}})
        return self._patient_dict(patient)

    async def update_order(
        self, order_id: str, payload: dict[str, Any], actor_id: str | None, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        from app.infrastructure.database.models import OrderRecord, StudyRecord

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
        for field in ("clinical_history", "comparative_study", "fasting_glucose", "injection_site", "creatinine"):
            if field in payload:
                setattr(order, field, (str(payload[field]).strip() or None) if payload[field] is not None else None)
        for field in ("height_cm", "weight_kg"):
            if field in payload:
                setattr(order, field, _to_float(payload[field]))
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
                    select(StudyRecord.study_instance_uid).where(StudyRecord.study_instance_uid == uid)
                )).scalar_one_or_none()
                if not exists:
                    raise OnboardingValidationError("study_instance_uid not found")
            order.study_instance_uid = uid

        await self._session.flush()
        await self._audit(actor_id, "order_updated", order.id, self._order_dict(order))
        return self._order_dict(order)

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
        height_cm = _to_float(payload.get("height_cm"))
        weight_kg = _to_float(payload.get("weight_kg"))

        # ── Validation (spec §4) ──────────────────────────────────────────────
        if not patient_ref:
            raise OnboardingValidationError("patient_ref is required")
        if sex not in SEX_VALUES:
            raise OnboardingValidationError(f"sex must be one of {sorted(SEX_VALUES)}")
        if age_band not in AGE_BANDS:
            raise OnboardingValidationError(f"age_band must be one of {sorted(AGE_BANDS)}")
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
                    StudyRecord.study_instance_uid == study_instance_uid
                )
            )).scalar_one_or_none()
            if not exists:
                raise OnboardingValidationError("study_instance_uid not found")
            linked_uid = study_instance_uid
        else:
            # Auto-link if exactly one study matches this MRN (deterministic).
            matches = (await self._session.execute(
                select(StudyRecord.study_instance_uid).where(StudyRecord.patient_id == patient_ref)
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
            creatinine=creatinine,
            created_by=actor_id or None, tenant_id=tenant_id,
        )
        self._session.add(order)
        await self._session.flush()

        if patient_is_new:
            await self._audit(actor_id, "patient_created", patient.id, {"patient_ref": patient_ref})
        await self._audit(
            actor_id, "order_created", order.id,
            {"patient_ref": patient_ref, "modality": modality, "study": linked_uid},
        )
        return {"order": self._order_dict(order), "patient": self._patient_dict(patient)}

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
            select(StudyRecord.study_instance_uid).where(StudyRecord.study_instance_uid == study_uid)
        )).scalar_one_or_none()
        if not exists:
            raise OnboardingValidationError("study_instance_uid not found")
        order.study_instance_uid = study_uid
        await self._session.flush()
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

    async def _audit(self, actor: str | None, action: str, entity_id: str, details: dict) -> None:
        from app.infrastructure.database.models import AuditLogRecord

        self._session.add(AuditLogRecord(
            id=str(uuid.uuid4()),
            action=action,
            entity_type="onboarding",
            entity_id=entity_id,
            actor=actor or "system",
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
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
