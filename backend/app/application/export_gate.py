from __future__ import annotations

"""AI results leave the platform only after a radiologist has signed them.

Human-in-the-loop gate (FDA assistive-software practice; HIPAA 164.306(a)(1) integrity):
an AI-generated draft is PRELIMINARY until attested. Every path that sends results to
another system — DICOM SR / SEG export, the FHIR DiagnosticReport, webhooks with findings —
asks this module first. With REQUIRE_SIGNED_FOR_EXPORT (default on) an export needs the
latest e-signature AND the signed content must be unchanged since signing (a re-run after
signing makes the report unsigned again in effect).

Agreed 164.522 restrictions (patient_rights_service) are checked here too: with a
``channel`` the export is refused while the patient restricts that channel.

Every refusal says what blocked it and how to unblock it: ``ExportNotSigned.detail()`` is
``{code, message, remedy, action}`` (action = a label + an in-app path to where it is fixed).

Application layer: no FastAPI imports; routers map ExportNotSigned to HTTP 409 with
``detail()`` as the body.
"""

from typing import Any

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


# What each outbound channel is called in messages.
CHANNEL_LABELS = {
    "fhir": "sending the report to the hospital system (FHIR)",
    "dicom_export": "exporting the report as DICOM (SR / SEG)",
    "webhooks": "notifying connected systems (webhooks)",
    "share_links": "sharing the report through a portal link",
}


class ExportNotSigned(Exception):
    """The report has no valid signature, so its AI results may not be exported."""

    code = "not_signed"

    def __init__(self, message: str, *, code: str | None = None, remedy: str = "",
                 action: dict[str, str] | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.remedy = remedy
        self.action = action

    def detail(self) -> dict[str, Any]:
        """The HTTP 409 body: what blocked it, how to fix it, where to go."""
        return {"code": self.code, "message": str(self), "remedy": self.remedy,
                "action": self.action}


class ExportRestricted(ExportNotSigned):
    """The patient has an agreed restriction on this disclosure channel (164.522)."""

    code = "patient_restriction"


def _signoff_action(study_uid: str) -> dict[str, str]:
    return {"label": "Open report sign-off", "path": f"/study/{study_uid}#signoff"}


async def require_unrestricted(session, tenant_id: str, study_uid: str, channel: str,
                               actor: str = "system") -> None:
    from app.application.patient_rights_service import DisclosureRestricted, PatientRightsService

    try:
        await PatientRightsService(session, tenant_id).require_unrestricted(study_uid, channel, actor)
    except DisclosureRestricted as exc:
        what = CHANNEL_LABELS.get(channel, "this disclosure")
        raise ExportRestricted(
            f"The patient asked for their data not to be shared this way, and the request was "
            f"agreed (HIPAA §164.522). While that restriction is active, {what} is blocked "
            f"for this patient.",
            remedy=(
                "Use a channel the patient has not restricted (for example, hand over a copy "
                "through Patient Rights → Export). The restriction can only be lifted if the "
                "patient withdraws it: a privacy officer then revokes it on Patient Rights → "
                "Restrictions, after which this works again."
            ),
            action={"label": "Open Patient Rights", "path": "/admin/patient-rights"},
        ) from exc


async def require_signed(session, tenant_id: str, study_uid: str,
                         channel: str | None = None, actor: str = "system") -> dict[str, Any] | None:
    """The valid latest signature of the study's report; raises ExportNotSigned otherwise.
    Returns None when the gate is switched off (REQUIRE_SIGNED_FOR_EXPORT=false).
    With ``channel``, a patient restriction on it raises ExportRestricted (always, gate
    switched off or not)."""
    from app.application.review_signoff_service import ReviewSignoffService

    if channel:
        await require_unrestricted(session, tenant_id, study_uid, channel, actor)
    if not get_settings().require_signed_for_export:
        return None
    what = CHANNEL_LABELS.get(channel or "", "sending it outside the platform")
    signature = await ReviewSignoffService(session, tenant_id=tenant_id).signature_for_report(study_uid)
    if signature is None:
        raise ExportNotSigned(
            f"This report has not been signed yet. Until a radiologist reviews and signs it, "
            f"its AI findings are preliminary, so {what} is not allowed.",
            remedy=(
                "A radiologist opens this study, reviews the AI findings and signs the report in "
                "the Report sign-off panel. Then try again."
            ),
            action=_signoff_action(study_uid),
        )
    from app.domain.npi import is_valid_npi

    if get_settings().require_npi_for_signing and not is_valid_npi(signature.get("signer_npi")):
        raise ExportNotSigned(
            f"The report was signed by {signature.get('signer_full_name') or 'a radiologist'} "
            f"without a valid NPI (National Provider Identifier) on file, so {what} is not "
            f"allowed: the receiving system needs to know who verified the report.",
            code="npi_missing",
            remedy=(
                "An administrator adds the radiologist's 10-digit NPI to their practitioner "
                "record (PATCH /api/practitioners/{user_id}); the radiologist then signs the "
                "report again."
            ),
            action=_signoff_action(study_uid),
        )
    if signature.get("integrity") != "valid":
        raise ExportNotSigned(
            f"The AI results changed after the report was signed (for example, the analysis "
            f"was run again), so the signature no longer covers what would be sent and {what} "
            f"is not allowed.",
            code="changed_since_signing",
            remedy=(
                "A radiologist reviews the updated results and signs the report again in the "
                "Report sign-off panel. Then try again."
            ),
            action=_signoff_action(study_uid),
        )
    return signature


def preliminary_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """What a ``result_ready`` webhook may carry before sign-off: that a result exists,
    not what it says. Findings follow in ``report_signed``."""
    if not get_settings().require_signed_for_export:
        return payload
    keep = ("study_instance_uid", "usecase_name", "result_id", "patient_id")
    return {**{k: payload[k] for k in keep if k in payload}, "report_status": "preliminary"}


async def notify_report_signed(session, tenant_id: str, study_uid: str) -> int:
    """After a signature: send ``report_signed`` webhooks with the signed findings of each
    use case. Never raises — a webhook problem must not undo a signature."""
    from sqlalchemy import select

    from app.application.alerting_service import AlertingService
    from app.application.patient_rights_service import PatientRightsService
    from app.infrastructure.database.models import ResultRecord

    try:
        if await PatientRightsService(session, tenant_id).study_restricted(study_uid, "webhooks"):
            logger.info("report_signed_webhook_restricted", study_uid=study_uid)
            return 0
        results = (await session.execute(
            select(ResultRecord).where(
                ResultRecord.study_instance_uid == study_uid,
                ResultRecord.tenant_id == tenant_id,
                ResultRecord.is_latest == True,  # noqa: E712
            )
        )).scalars().all()
        sent = 0
        alerting = AlertingService(session)
        for r in results:
            sent += await alerting.trigger_alert("report_signed", {
                "study_instance_uid": study_uid,
                "usecase_name": r.usecase_name,
                "result_id": r.id,
                "report_status": "final",
                "qa_flags": r.qa_flags or [],
                "measurements": r.measurements or {},
            })
        await session.commit()
        return sent
    except Exception as exc:
        logger.warning("report_signed_webhook_failed", study_uid=study_uid, error=str(exc))
        return 0
