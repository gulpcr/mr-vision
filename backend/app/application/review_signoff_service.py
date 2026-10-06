"""Priority review queue, electronic report signatures and report comments.

* Review queue — every study with at least one AI result that is not yet signed,
  ordered critical › abnormal › normal (domain/report_priority.py), then oldest first.
  A radiologist may override a study's computed priority.
* E-signature — the signer affirms a versioned attestation statement
  (domain/signature_statements.py) and adds a mandatory comment. The signature stores
  the exact statement text, the signer's identity at signing time and a SHA-256 of the
  signed content (latest AI results + editable mammography report), so a later change
  to that content is detectable. Signing moves the study to ``signed`` from any unsigned
  state (it subsumes report → sign).
* Comments — radiologists and referring doctors may comment on a report.

Tenant and referral boundaries: Row-Level Security confines every query to the caller's
workspace (and a referring doctor to their referred studies); the explicit tenant /
referral filters below are the application-level half.

Application layer — no FastAPI imports; the router maps exceptions to HTTP codes.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import structlog
from sqlalchemy import select

from app.domain.interfaces import ArtifactStore, AuditRepository
from app.domain.models import AuditEntry, utcnow
from app.domain.report_priority import PRIORITIES, RANK, compute_priority
from app.domain.signature_statements import (
    CURRENT_STATEMENT_VERSION,
    STATEMENTS,
    render_statement,
)
from app.config import get_settings
from app.domain.patient_identity import displayed_patient_name

logger = structlog.get_logger(__name__)

SIGNED = "signed"
MAX_COMMENT = 4000
QUEUE_LIMIT = 2000


class StudyNotFoundError(Exception):
    """Study not found in the caller's scope (→ 404)."""


class SignoffConflictError(Exception):
    """The study cannot be signed in its current state (→ 409)."""


class SignoffForbiddenError(Exception):
    """The actor may not sign this study (→ 403)."""


class SignedDocumentIntegrityError(Exception):
    """A frozen signed PDF no longer matches its recorded SHA-256."""


class SignoffValidationError(Exception):
    """Invalid signature / comment / priority input (→ 422)."""


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def content_hash(snapshot: dict[str, Any]) -> str:
    """Canonical SHA-256 of a content snapshot (sorted keys, compact separators)."""
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReviewSignoffService:
    def __init__(
        self,
        session,
        tenant_id: str,
        user_id: str = "",
        username: str = "system",
        referring_user_id: str | None = None,
        audit_repo: AuditRepository | None = None,
    ):
        self._session = session
        self._audit_repo = audit_repo
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._username = username
        self._referring_user_id = referring_user_id

    # ── Scope helpers ──────────────────────────────────────────────────────────

    def _study_scope(self, stmt):
        from app.infrastructure.database.models import StudyRecord

        stmt = stmt.where(StudyRecord.tenant_id == self._tenant_id)
        if self._referring_user_id:
            from app.infrastructure.database.access_scope import referral_visible

            stmt = stmt.where(referral_visible(self._referring_user_id))
        return stmt

    async def _get_study(self, study_uid: str):
        from app.infrastructure.database.models import StudyRecord

        stmt = self._study_scope(
            select(StudyRecord).where(StudyRecord.study_instance_uid == study_uid)
        )
        rec = (await self._session.execute(stmt)).scalar_one_or_none()
        if rec is None:
            raise StudyNotFoundError(study_uid)
        return rec

    async def _latest_results(self, study_uids: list[str]) -> dict[str, list]:
        from app.infrastructure.database.models import ResultRecord

        out: dict[str, list] = {uid: [] for uid in study_uids}
        if not study_uids:
            return out
        rows = (await self._session.execute(
            select(ResultRecord).where(
                ResultRecord.study_instance_uid.in_(study_uids),
                ResultRecord.is_latest == True,  # noqa: E712
                ResultRecord.tenant_id == self._tenant_id,
            ).order_by(ResultRecord.usecase_name)
        )).scalars().all()
        for r in rows:
            out[r.study_instance_uid].append(r)
        return out

    async def _alerts(self, study_uids: list[str]) -> dict[str, list[dict[str, Any]]]:
        from app.infrastructure.database.models import CriticalAlertRecord

        out: dict[str, list[dict[str, Any]]] = {uid: [] for uid in study_uids}
        if not study_uids:
            return out
        rows = (await self._session.execute(
            select(
                CriticalAlertRecord.study_instance_uid, CriticalAlertRecord.severity,
                CriticalAlertRecord.finding_type, CriticalAlertRecord.title,
            ).where(
                CriticalAlertRecord.study_instance_uid.in_(study_uids),
                CriticalAlertRecord.tenant_id == self._tenant_id,
            )
        )).all()
        for uid, severity, finding_type, title in rows:
            out[uid].append({"severity": severity, "finding_type": finding_type, "title": title})
        return out

    @staticmethod
    def _priority(study, alerts, results) -> dict[str, Any]:
        computed, reasons = compute_priority(
            alerts, [(r.usecase_name, r.summary or {}) for r in results]
        )
        return {
            "priority": study.priority_override or computed,
            "computed_priority": computed,
            "priority_reasons": reasons,
            "priority_overridden": bool(study.priority_override),
            "priority_override_by": study.priority_override_by,
            "priority_override_at": _iso(study.priority_override_at),
        }

    # ── Review queue ───────────────────────────────────────────────────────────

    async def queue(self, status: str = "unsigned", priority: str | None = None) -> dict[str, Any]:
        """Studies with ≥1 AI result. ``status``: unsigned (default) | signed."""
        from app.infrastructure.database.models import ReportSignatureRecord, ResultRecord, StudyRecord

        has_result = select(ResultRecord.study_instance_uid).where(
            ResultRecord.study_instance_uid == StudyRecord.study_instance_uid,
            ResultRecord.is_latest == True,  # noqa: E712
        ).exists()
        stmt = self._study_scope(select(StudyRecord).where(has_result))
        if status == "signed":
            stmt = stmt.where(StudyRecord.reading_status == SIGNED).order_by(StudyRecord.signed_at.desc())
        else:
            stmt = stmt.where(StudyRecord.reading_status != SIGNED).order_by(StudyRecord.created_at.asc())
        studies = (await self._session.execute(stmt.limit(QUEUE_LIMIT))).scalars().all()

        uids = [s.study_instance_uid for s in studies]
        results = await self._latest_results(uids)
        alerts = await self._alerts(uids)
        signers: dict[str, tuple[str, str]] = {}
        if status == "signed" and uids:
            rows = (await self._session.execute(
                select(
                    ReportSignatureRecord.study_instance_uid,
                    ReportSignatureRecord.signer_full_name,
                    ReportSignatureRecord.signed_at,
                ).where(ReportSignatureRecord.study_instance_uid.in_(uids))
                .order_by(ReportSignatureRecord.signed_at.asc())
            )).all()
            for uid, name, signed_at in rows:           # ascending → the latest wins
                signers[uid] = (name, _iso(signed_at))

        items = []
        counts = {p: 0 for p in PRIORITIES}
        for s in studies:
            info = self._priority(s, alerts[s.study_instance_uid], results[s.study_instance_uid])
            counts[info["priority"]] += 1
            if priority and info["priority"] != priority:
                continue
            signer = signers.get(s.study_instance_uid)
            items.append({
                "study_instance_uid": s.study_instance_uid,
                "patient_name": displayed_patient_name(
                    s.patient_name, s.patient_id, get_settings().display_patient_names
                ),
                "patient_id": s.patient_id,
                "modality": s.modality,
                "body_part_examined": s.body_part_examined,
                "study_description": s.study_description,
                "study_date": _iso(s.study_date),
                "received_at": _iso(s.created_at),
                "reading_status": s.reading_status,
                "assigned_to_username": s.assigned_to_username,
                "signed_at": _iso(s.signed_at),
                "signed_by": signer[0] if signer else None,
                "usecases": [r.usecase_name for r in results[s.study_instance_uid]],
                **info,
            })
        if status != "signed":
            # Stable sort: priority first, then the oldest-received order from SQL.
            items.sort(key=lambda i: RANK[i["priority"]])
        return {"status": status, "counts": counts, "items": items}

    async def set_priority(self, study_uid: str, priority: str | None) -> dict[str, Any]:
        if priority is not None and priority not in PRIORITIES:
            raise SignoffValidationError(f"priority must be one of {', '.join(PRIORITIES)} or null")
        study = await self._get_study(study_uid)
        previous = study.priority_override
        study.priority_override = priority
        study.priority_override_by = self._username if priority else None
        study.priority_override_at = utcnow() if priority else None
        await self._audit("study_priority_changed", study_uid, {"from": previous, "to": priority})
        await self._session.flush()
        return await self.get_signoff(study_uid)

    # ── Signature ──────────────────────────────────────────────────────────────

    async def _snapshot(self, study_uid: str, results: list) -> dict[str, Any]:
        from app.infrastructure.database.models import (
            AIInferenceMetadataRecord,
            MammographyReportRecord,
        )

        provenance: dict[str, str] = {}
        if results:
            provenance = dict((await self._session.execute(
                select(AIInferenceMetadataRecord.result_id, AIInferenceMetadataRecord.record_sha256)
                .where(AIInferenceMetadataRecord.result_id.in_([r.id for r in results]))
            )).all())
        snapshot: dict[str, Any] = {
            "study_instance_uid": study_uid,
            "results": [
                {
                    "result_id": r.id,
                    "usecase_name": r.usecase_name,
                    "version": r.version,
                    "model_version": r.model_version,
                    "model_checksum": r.model_checksum,
                    "summary": r.summary or {},
                    "measurements": r.measurements or {},
                    "qa_flags": r.qa_flags or [],
                    # The exact model iteration (AI-02), when recorded.
                    **({"provenance_sha256": provenance[r.id]} if r.id in provenance else {}),
                }
                for r in results
            ],
        }
        mammo = (await self._session.execute(
            select(MammographyReportRecord).where(
                MammographyReportRecord.study_instance_uid == study_uid,
                MammographyReportRecord.tenant_id == self._tenant_id,
            )
        )).scalar_one_or_none()
        if mammo is not None:
            skip = {"created_at", "updated_at", "created_by", "tenant_id"}
            snapshot["mammography_report"] = {
                c.name: getattr(mammo, c.name) for c in mammo.__table__.columns if c.name not in skip
            }
        return snapshot

    async def _signer(self):
        from app.infrastructure.database.models import UserRecord

        user = (await self._session.execute(
            select(UserRecord).where(UserRecord.id == self._user_id, UserRecord.tenant_id == self._tenant_id)
        )).scalar_one_or_none()
        if user is None:
            raise SignoffForbiddenError("Only a workspace user account can sign reports")
        return user

    async def sign(
        self,
        study_uid: str,
        *,
        statement_version: str,
        agreed: bool,
        comment: str,
        full_name: str | None,
        is_admin: bool,
        client_ip: str | None = None,
    ) -> dict[str, Any]:
        from app.infrastructure.database.models import ReportSignatureRecord

        if not agreed:
            raise SignoffValidationError("You must affirm the attestation statement to sign")
        if statement_version != CURRENT_STATEMENT_VERSION:
            raise SignoffValidationError(
                "The attestation statement has changed — reload the page and review it again"
            )
        comment = (comment or "").strip()
        if not comment:
            raise SignoffValidationError("A signing comment is required")
        if len(comment) > MAX_COMMENT:
            raise SignoffValidationError(f"The comment is longer than {MAX_COMMENT} characters")

        study = await self._get_study(study_uid)
        if study.reading_status == SIGNED:
            raise SignoffConflictError("This report is already signed")
        if study.assigned_to and study.assigned_to != self._user_id and not is_admin:
            raise SignoffForbiddenError(
                f"This study is assigned to {study.assigned_to_username or 'another radiologist'}"
            )
        results = (await self._latest_results([study_uid]))[study_uid]
        if not results:
            raise SignoffConflictError("There is no AI result to sign for this study yet")

        user = await self._signer()
        from app.domain.npi import NPI_SYSTEM, is_valid_npi

        signer_npi = user.identifier_value if (
            user.identifier_system == NPI_SYSTEM and is_valid_npi(user.identifier_value)
        ) else None
        if get_settings().require_npi_for_signing and signer_npi is None:
            raise SignoffValidationError(
                "A valid NPI must be recorded on your practitioner profile before you can sign"
            )
        name = (user.full_name or "").strip()
        if not name:
            name = (full_name or "").strip()
            if len(name) < 3:
                raise SignoffValidationError("Enter your full name as it should appear on the signature")
            user.full_name = name[:256]
            await self._audit("user_profile_updated", user.id, {"field": "full_name", "via": "e-signature"},
                              entity_type="user")

        alerts = (await self._alerts([study_uid]))[study_uid]
        priority = self._priority(study, alerts, results)["priority"]
        snapshot = await self._snapshot(study_uid, results)
        digest = content_hash(snapshot)
        now = utcnow()
        signature = ReportSignatureRecord(
            id=str(uuid.uuid4()),
            tenant_id=self._tenant_id,
            study_instance_uid=study_uid,
            signer_user_id=user.id,
            signer_username=user.username,
            signer_full_name=name,
            signer_role=user.role,
            signer_npi=signer_npi,
            statement_version=statement_version,
            statement_text=render_statement(name, statement_version),
            comment=comment,
            content_hash=digest,
            content_snapshot=snapshot,
            priority_at_signing=priority,
            client_ip=(client_ip or "")[:64] or None,
            signed_at=now,
        )
        self._session.add(signature)
        await self._session.flush()  # the metrics rows reference the signature
        await self._record_review_metrics(signature, study, snapshot)

        if not study.assigned_to:
            study.assigned_to = user.id
            study.assigned_to_username = user.username
            study.assigned_at = now
        study.reported_at = study.reported_at or now
        study.signed_at = now
        study.reading_status = SIGNED
        await self._audit("report_esigned", study_uid, {
            "signature_id": signature.id,
            "statement_version": statement_version,
            "content_hash": digest,
            "priority": priority,
        })
        await self._session.flush()
        logger.info("report_esigned", study_uid=study_uid, signer=user.username, priority=priority)
        return await self.get_signoff(study_uid)

    # ── Sign-off state ─────────────────────────────────────────────────────────

    async def get_signoff(self, study_uid: str) -> dict[str, Any]:
        from app.infrastructure.database.models import ReportSignatureRecord, UserRecord

        study = await self._get_study(study_uid)
        results = (await self._latest_results([study_uid]))[study_uid]
        alerts = (await self._alerts([study_uid]))[study_uid]
        signatures = (await self._session.execute(
            select(ReportSignatureRecord).where(
                ReportSignatureRecord.study_instance_uid == study_uid,
                ReportSignatureRecord.tenant_id == self._tenant_id,
            ).order_by(ReportSignatureRecord.signed_at.desc())
        )).scalars().all()

        integrity = None
        if signatures:
            current = content_hash(await self._snapshot(study_uid, results))
            integrity = "valid" if current == signatures[0].content_hash else "changed"

        my_name = ""
        if self._user_id:
            my_name = (await self._session.execute(
                select(UserRecord.full_name).where(UserRecord.id == self._user_id)
            )).scalar_one_or_none() or ""

        return {
            "study_instance_uid": study_uid,
            "reading_status": study.reading_status,
            "assigned_to": study.assigned_to,
            "assigned_to_username": study.assigned_to_username,
            "signed_at": _iso(study.signed_at),
            "has_results": bool(results),
            **self._priority(study, alerts, results),
            "signatures": [self._signature_dict(s) for s in signatures],
            "integrity": integrity,
            "statement": {
                "version": CURRENT_STATEMENT_VERSION,
                "template": STATEMENTS[CURRENT_STATEMENT_VERSION],
                "signer_full_name": my_name.strip(),
                "text": render_statement(my_name) if my_name.strip() else None,
            },
            "comments": await self.list_comments(study_uid, _checked=True),
        }

    @staticmethod
    def _signature_dict(s) -> dict[str, Any]:
        return {
            "id": s.id,
            "signer_user_id": s.signer_user_id,
            "signer_username": s.signer_username,
            "signer_full_name": s.signer_full_name,
            "signer_role": s.signer_role,
            "signer_npi": getattr(s, "signer_npi", None),
            "statement_version": s.statement_version,
            "statement_text": s.statement_text,
            "comment": s.comment,
            "content_hash": s.content_hash,
            "priority_at_signing": s.priority_at_signing,
            "signed_at": _iso(s.signed_at),
        }

    async def signature_for_report(self, study_uid: str) -> dict[str, Any] | None:
        """Latest signature + whether the content still matches it (for PDF rendering)."""
        from app.infrastructure.database.models import ReportSignatureRecord

        rec = (await self._session.execute(
            select(ReportSignatureRecord).where(
                ReportSignatureRecord.study_instance_uid == study_uid,
                ReportSignatureRecord.tenant_id == self._tenant_id,
            ).order_by(ReportSignatureRecord.signed_at.desc()).limit(1)
        )).scalar_one_or_none()
        if rec is None:
            return None
        results = (await self._latest_results([study_uid]))[study_uid]
        current = content_hash(await self._snapshot(study_uid, results))
        return {**self._signature_dict(rec), "integrity": "valid" if current == rec.content_hash else "changed"}

    # ── Comments ───────────────────────────────────────────────────────────────

    async def list_comments(self, study_uid: str, _checked: bool = False) -> list[dict[str, Any]]:
        from app.infrastructure.database.models import ReportCommentRecord

        if not _checked:
            await self._get_study(study_uid)
        rows = (await self._session.execute(
            select(ReportCommentRecord).where(
                ReportCommentRecord.study_instance_uid == study_uid,
                ReportCommentRecord.tenant_id == self._tenant_id,
            ).order_by(ReportCommentRecord.created_at.asc())
        )).scalars().all()
        return [
            {
                "id": c.id,
                "author_username": c.author_username,
                "author_full_name": c.author_full_name,
                "author_role": c.author_role,
                "body": c.body,
                "created_at": _iso(c.created_at),
            }
            for c in rows
        ]

    async def add_comment(self, study_uid: str, body: str) -> dict[str, Any]:
        from app.infrastructure.database.models import ReportCommentRecord

        body = (body or "").strip()
        if not body:
            raise SignoffValidationError("The comment is empty")
        if len(body) > MAX_COMMENT:
            raise SignoffValidationError(f"The comment is longer than {MAX_COMMENT} characters")
        await self._get_study(study_uid)
        user = await self._signer()
        rec = ReportCommentRecord(
            id=str(uuid.uuid4()),
            tenant_id=self._tenant_id,
            study_instance_uid=study_uid,
            author_user_id=user.id,
            author_username=user.username,
            author_full_name=(user.full_name or "").strip() or None,
            author_role=user.role,
            body=body,
            created_at=utcnow(),
        )
        self._session.add(rec)
        await self._audit("report_comment_added", study_uid, {"comment_id": rec.id})
        await self._session.flush()
        return {
            "id": rec.id,
            "author_username": rec.author_username,
            "author_full_name": rec.author_full_name,
            "author_role": rec.author_role,
            "body": rec.body,
            "created_at": _iso(rec.created_at),
        }

    # ── Audit ──────────────────────────────────────────────────────────────────

    async def _record_review_metrics(self, signature, study, snapshot: dict[str, Any]) -> None:
        """AI-04: AI draft vs signed report, per result (numbers only). Never blocks signing."""
        from app.domain.report_edit_metrics import review_metrics
        from app.infrastructure.database.models import AIReportReviewRecord

        try:
            signed_report = snapshot.get("mammography_report")
            for r in snapshot.get("results", []):
                m = review_metrics(r.get("summary") or {},
                                   signed_report if r.get("usecase_name") == "mammography" else None)
                self._session.add(AIReportReviewRecord(
                    id=str(uuid.uuid4()), tenant_id=self._tenant_id, signature_id=signature.id,
                    result_id=r["result_id"], usecase_name=r["usecase_name"],
                    modality=getattr(study, "modality", None), model_version=r.get("model_version"),
                    **m,
                ))
        except Exception as exc:
            logger.warning("ai_review_metrics_failed", study_uid=study.study_instance_uid, error=str(exc))

    # ── Frozen signed documents (alembic 054) ──────────────────────────────────

    async def _latest_signature(self, study_uid: str):
        from app.infrastructure.database.models import ReportSignatureRecord

        return (await self._session.execute(
            select(ReportSignatureRecord).where(
                ReportSignatureRecord.study_instance_uid == study_uid,
                ReportSignatureRecord.tenant_id == self._tenant_id,
            ).order_by(ReportSignatureRecord.signed_at.desc()).limit(1)
        )).scalar_one_or_none()

    async def _document(self, signature_id: str, usecase: str):
        from app.infrastructure.database.models import ReportSignatureDocumentRecord

        return (await self._session.execute(
            select(ReportSignatureDocumentRecord).where(
                ReportSignatureDocumentRecord.signature_id == signature_id,
                ReportSignatureDocumentRecord.usecase == usecase,
            )
        )).scalar_one_or_none()

    async def signed_document(self, study_uid: str, usecase: str, store: ArtifactStore) -> bytes | None:
        """The frozen PDF of the latest signature for this report, hash-verified; None
        if the study is unsigned or no copy has been frozen yet. Raises
        SignedDocumentIntegrityError (and audits it) if the stored bytes changed."""
        signature = await self._latest_signature(study_uid)
        if signature is None:
            return None
        doc = await self._document(signature.id, usecase)
        if doc is None:
            return None
        data = await store.get(doc.object_path)
        if hashlib.sha256(data).hexdigest() != doc.sha256:
            await self._audit("signed_document_integrity_failed", study_uid, {
                "signature_id": signature.id, "usecase": usecase, "expected_sha256": doc.sha256,
            })
            raise SignedDocumentIntegrityError(
                "The stored signed report does not match its signature record"
            )
        return data

    async def freeze_signed_document(
        self, study_uid: str, usecase: str, pdf: bytes, store: ArtifactStore
    ) -> str | None:
        """Store ``pdf`` as the signed copy of this report if the study is signed, its
        content still matches the signature, and no copy exists yet. Returns the hash."""
        from app.infrastructure.database.models import ReportSignatureDocumentRecord

        signature = await self._latest_signature(study_uid)
        if signature is None or await self._document(signature.id, usecase) is not None:
            return None
        results = (await self._latest_results([study_uid]))[study_uid]
        if content_hash(await self._snapshot(study_uid, results)) != signature.content_hash:
            logger.warning("signed_document_not_frozen_content_changed", study_uid=study_uid,
                           usecase=usecase)
            return None
        digest = hashlib.sha256(pdf).hexdigest()
        path = f"{self._tenant_id}/{study_uid}/signed/{signature.id}/{usecase}.pdf"
        await store.put(path, pdf, "application/pdf")
        self._session.add(ReportSignatureDocumentRecord(
            id=str(uuid.uuid4()), tenant_id=self._tenant_id, signature_id=signature.id,
            study_instance_uid=study_uid, usecase=usecase, sha256=digest,
            object_path=path, size_bytes=len(pdf),
        ))
        await self._audit("signed_document_frozen", study_uid, {
            "signature_id": signature.id, "usecase": usecase, "sha256": digest,
        })
        await self._session.commit()
        return digest

    async def verify_signature(self, study_uid: str, store: ArtifactStore) -> dict[str, Any]:
        """Integrity of the latest signature: the signed content (snapshot hash) and
        every frozen PDF (stored bytes vs recorded SHA-256)."""
        from app.infrastructure.database.models import ReportSignatureDocumentRecord

        signature = await self._latest_signature(study_uid)
        if signature is None:
            return {"study_instance_uid": study_uid, "signed": False}
        results = (await self._latest_results([study_uid]))[study_uid]
        content_ok = content_hash(await self._snapshot(study_uid, results)) == signature.content_hash
        docs = (await self._session.execute(
            select(ReportSignatureDocumentRecord).where(
                ReportSignatureDocumentRecord.signature_id == signature.id
            )
        )).scalars().all()
        documents = []
        for doc in docs:
            try:
                ok = hashlib.sha256(await store.get(doc.object_path)).hexdigest() == doc.sha256
            except Exception:
                ok = False
            documents.append({"usecase": doc.usecase, "sha256": doc.sha256, "valid": ok,
                              "frozen_at": _iso(doc.created_at)})
        valid = content_ok and all(d["valid"] for d in documents)
        await self._audit("signature_verified", study_uid, {
            "signature_id": signature.id, "valid": valid,
        })
        return {
            "study_instance_uid": study_uid,
            "signed": True,
            "signature_id": signature.id,
            "signed_at": _iso(signature.signed_at),
            "signer_full_name": signature.signer_full_name,
            "content_hash": signature.content_hash,
            "content_valid": content_ok,
            "documents": documents,
            "valid": valid,
        }

    async def _audit(self, action: str, entity_id: str, details: dict, entity_type: str = "study_reading") -> None:
        """Hash-chained audit entry (via the injected AuditRepository). Signatures are
        legally significant, so they must be in the tamper-evident chain."""
        if self._audit_repo is None:
            logger.warning("review_signoff_audit_unavailable", action=action, entity_id=entity_id)
            return
        await self._audit_repo.save(AuditEntry(
            tenant_id=self._tenant_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=self._username,
            details={**details, "user_id": self._user_id},
        ))
