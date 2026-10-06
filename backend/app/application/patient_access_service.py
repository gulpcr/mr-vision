from __future__ import annotations

"""Patient rights: right of access (HIPAA 164.524) and accounting of disclosures /
access report (164.528).

* ``export_record`` builds a ZIP of everything the workspace holds for one patient
  (MRN): a manifest with demographics and studies, each result (summary, measurements,
  report text) as JSON, a PDF report per result, the stored AI artifacts, and — when
  requested — the original DICOM images from the PACS. The export itself is recorded as
  a disclosure (``patient_record_disclosed``).
* ``access_report`` lists, from the tamper-evident audit log, every access to and
  disclosure of the patient's data: who, when, what, from where. Disclosures (records
  leaving the workspace: exports, share links, FHIR/DICOM exports, emergency access) are
  reported separately from routine internal access.
"""

import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import and_, or_, select

from app.domain.enums import AuditAction
from app.domain.models import AuditEntry

logger = structlog.get_logger(__name__)

# Audit actions that mean the record left the workspace (disclosures, 164.528).
DISCLOSURE_ACTIONS = frozenset({
    "patient_record_disclosed", "share_link_redeemed", "result_exported",
    "report_downloaded", "break_glass_invoked",
})
ACCESS_ACTIONS = frozenset({"phi_accessed", "result_viewed", "study_deleted"}) | DISCLOSURE_ACTIONS



MIN_PASSPHRASE = 12


class WeakPassphrase(ValueError):
    pass


def encrypt_zip(data: bytes, passphrase: str) -> bytes:
    """Re-pack a ZIP with AES-256 encryption on every entry (pyzipper, WinZip AE-2)."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise WeakPassphrase(f"The passphrase must be at least {MIN_PASSPHRASE} characters")
    import pyzipper

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, pyzipper.AESZipFile(
        out, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES,
    ) as dst:
        dst.setpassword(passphrase.encode("utf-8"))
        dst.setencryption(pyzipper.WZ_AES, nbits=256)
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info))
    return out.getvalue()

class PatientNotFound(LookupError):
    pass


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class PatientAccessService:
    def __init__(self, session, tenant_id: str, artifact_store=None, pacs=None):
        self._session = session
        self._tenant_id = tenant_id
        self._store = artifact_store
        self._pacs = pacs

    async def _studies(self, mrn: str):
        from app.infrastructure.database.models import StudyRecord

        rows = (
            await self._session.execute(
                select(StudyRecord).where(
                    StudyRecord.tenant_id == self._tenant_id, StudyRecord.patient_id == mrn
                ).order_by(StudyRecord.study_date.desc().nullslast())
            )
        ).scalars().all()
        if not rows:
            raise PatientNotFound(mrn)
        return rows

    async def _latest_results(self, study_uids: list[str]):
        from app.infrastructure.database.models import ResultRecord

        return (
            await self._session.execute(
                select(ResultRecord).where(
                    ResultRecord.tenant_id == self._tenant_id,
                    ResultRecord.study_instance_uid.in_(study_uids),
                    ResultRecord.is_latest == True,  # noqa: E712
                )
            )
        ).scalars().all()

    # ── Right of access ────────────────────────────────────────────────────────

    async def export_record(
        self, mrn: str, *, requester: str, actor: str, include_images: bool = False,
        passphrase: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """The patient's record as a ZIP. With ``passphrase`` every entry is AES-256
        encrypted (WinZip AE-2, opens in 7-Zip / macOS Archive Utility / WinZip), for a
        copy that leaves the platform on a USB stick or by e-mail; the passphrase is
        given to the patient separately and never stored or logged."""
        from app.application.report_service import generate_report_pdf
        from app.infrastructure.database.repositories import PgAuditRepository

        if passphrase and len(passphrase) < MIN_PASSPHRASE:
            raise WeakPassphrase(f"The passphrase must be at least {MIN_PASSPHRASE} characters")
        studies = await self._studies(mrn)
        uids = [s.study_instance_uid for s in studies]
        results = await self._latest_results(uids)

        manifest: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "patient": {"mrn": mrn, "name": studies[0].patient_name, "sex": studies[0].patient_sex,
                        "age": studies[0].patient_age},
            "requested_by": requester,
            "studies": [],
            "note": ("AI-generated findings are decision support, reviewed by a radiologist; "
                     "the signed report is the clinical record."),
        }
        buf = io.BytesIO()
        counts = {"studies": len(studies), "results": 0, "artifacts": 0, "dicom_studies": 0}
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for s in studies:
                manifest["studies"].append({
                    "study_instance_uid": s.study_instance_uid, "date": _iso(s.study_date),
                    "description": s.study_description, "modality": s.modality,
                    "institution": s.institution_name, "reading_status": s.reading_status,
                })
            for r in results:
                base = f"studies/{r.study_instance_uid}/{r.usecase_name}"
                zf.writestr(f"{base}/result.json", json.dumps({
                    "usecase": r.usecase_name, "created_at": _iso(r.created_at),
                    "model_version": r.model_version, "summary": r.summary,
                    "measurements": r.measurements, "qa_flags": r.qa_flags,
                }, indent=2, default=str))
                counts["results"] += 1
                study = next((s for s in studies if s.study_instance_uid == r.study_instance_uid), None)
                try:
                    pdf = await asyncio.to_thread(
                        generate_report_pdf,
                        study_uid=r.study_instance_uid,
                        patient_name=study.patient_name if study else None,  # the patient's own copy
                        patient_id=mrn, study_date=study.study_date if study else None,
                        study_description=study.study_description if study else None,
                        institution=study.institution_name if study else None,
                        usecase_name=r.usecase_name, model_version=r.model_version or "",
                        measurements=r.measurements or {}, summary=r.summary or {},
                        qa_flags=list(r.qa_flags or []), result_created_at=r.created_at,
                    )
                    zf.writestr(f"{base}/report.pdf", pdf)
                except Exception as exc:
                    logger.warning("access_export_pdf_failed", usecase=r.usecase_name, error=type(exc).__name__)
                if self._store is not None:
                    for a in r.artifacts or []:
                        try:
                            data = await self._store.get(a["storage_path"])
                        except Exception:
                            continue
                        zf.writestr(f"{base}/artifacts/{a['name']}", data)
                        counts["artifacts"] += 1
            if include_images and self._pacs is not None:
                for s in studies:
                    try:
                        archive = await self._pacs.download_study_archive(s.study_instance_uid)
                    except Exception as exc:
                        logger.warning("access_export_dicom_failed", error=type(exc).__name__)
                        continue
                    zf.writestr(f"studies/{s.study_instance_uid}/dicom.zip", archive)
                    counts["dicom_studies"] += 1
            manifest["contents"] = counts
            if self._store is None:
                manifest["incomplete"] = ["AI image artifacts (object store unavailable)"]
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))

        data = buf.getvalue()
        if passphrase:
            data = await asyncio.to_thread(encrypt_zip, data, passphrase)
        await PgAuditRepository(self._session, tenant_id=self._tenant_id).save(AuditEntry(
            action=AuditAction.PATIENT_RECORD_DISCLOSED, entity_type="patient", entity_id=mrn,
            actor=actor, details={"requested_by": requester, "include_images": include_images,
                                  "encrypted": bool(passphrase), **counts},
            tenant_id=self._tenant_id,
        ))
        return data, counts

    # ── Accounting of disclosures / access report ──────────────────────────────

    async def access_report(self, mrn: str, days: int = 6 * 365) -> dict[str, Any]:
        from app.infrastructure.database.models import AuditLogRecord, ResultRecord

        studies = await self._studies(mrn)
        uids = [s.study_instance_uid for s in studies]
        result_ids = list((
            await self._session.execute(
                select(ResultRecord.id).where(
                    ResultRecord.tenant_id == self._tenant_id,
                    ResultRecord.study_instance_uid.in_(uids),
                )
            )
        ).scalars())
        since = datetime.now(timezone.utc) - timedelta(days=days)
        concerns_patient = or_(
            and_(AuditLogRecord.entity_type == "patient", AuditLogRecord.entity_id == mrn),
            and_(AuditLogRecord.entity_type == "study", AuditLogRecord.entity_id.in_(uids)),
            and_(AuditLogRecord.entity_type == "result", AuditLogRecord.entity_id.in_(result_ids or [""])),
            AuditLogRecord.details["study_instance_uid"].as_string().in_(uids),
        )
        rows = (
            await self._session.execute(
                select(AuditLogRecord).where(
                    AuditLogRecord.tenant_id == self._tenant_id,
                    AuditLogRecord.timestamp >= since,
                    AuditLogRecord.action.in_(sorted(ACCESS_ACTIONS)),
                    concerns_patient,
                ).order_by(AuditLogRecord.timestamp.desc()).limit(5000)
            )
        ).scalars().all()

        def entry(r) -> dict[str, Any]:
            details = r.details or {}
            return {
                "timestamp": _iso(r.timestamp), "action": r.action,
                "user": r.actor_display or r.actor, "user_id": r.actor_id,
                "entity_type": r.entity_type, "entity_id": r.entity_id,
                "route": details.get("route"), "client_ip": r.client_ip or details.get("client_ip"),
                "details": {k: v for k, v in details.items() if k not in ("route", "client_ip")},
            }

        entries = [entry(r) for r in rows]
        return {
            "patient_mrn": mrn,
            "period_days": days,
            "disclosures": [e for e in entries if e["action"] in DISCLOSURE_ACTIONS],
            "accesses": [e for e in entries if e["action"] not in DISCLOSURE_ACTIONS],
        }
