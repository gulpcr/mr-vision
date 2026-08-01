from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
import httpx

from app.config import get_settings
from app.domain.fhir_terminology import (
    DATA_OPERATION_SYSTEM,
    DIAGNOSTIC_SERVICE_SECTION_SYSTEM,
    DICOM_UID_SYSTEM,
    LOINC_SYSTEM,
    MODEL_CHECKSUM_IDENTIFIER_SYSTEM,
    PROVENANCE_PARTICIPANT_TYPE_SYSTEM,
    dicom_uid_identifier,
    identifier,
)
from app.domain.models import utcnow

logger = structlog.get_logger(__name__)


# ── Clinical-safety gate: DiagnosticReport.status ─────────────────────────────
# FHIR R4 makes DiagnosticReport.status mandatory (1..1), and "final" asserts to the
# receiving system that the report was verified and released to the patient's chart.
# It is therefore derived from the radiologist reading workflow (studies.reading_status)
# and NEVER hardcoded — publishing unreviewed AI output as "final" is a patient-safety
# fault, not a conformance one. An unrecognised reading_status refuses the export
# outright rather than falling back to a more-final-looking value (fail closed).
_READING_STATUS_TO_FHIR: dict[str, str] = {
    "unread": "preliminary",
    "in_progress": "preliminary",
    "reported": "preliminary",
    "signed": "final",
}


def to_fhir_instant(value: datetime) -> str:
    """Serialise a datetime as a FHIR R4 ``instant``.

    ``instant`` requires a timezone offset — a naive value renders as
    "2026-07-29T00:00:00" and is rejected by every validator. All DateTime columns are
    timestamptz as of migration 027, so a naive value reaching here means something
    fabricated it (``datetime.utcnow()``, ``datetime.now()``, or a stripped tzinfo);
    that is a bug to fix at the source, not to paper over by assuming UTC.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"cannot serialise naive datetime {value!r} as a FHIR instant: an instant "
            "requires a timezone offset — use app.domain.models.utcnow()"
        )
    return value.isoformat()


class FHIRExportRefusedError(Exception):
    """A result must not be published because its review state is unknown.

    Distinct from a transport failure: the payload was never sent because emitting it
    would have misrepresented the report's clinical status.
    """


def resolve_report_status(reading_status: str | None, is_latest: bool = True) -> str:
    """Map studies.reading_status → DiagnosticReport.status.

    A superseded result (``is_latest=False``) retracts whatever was published for it,
    which is what ``entered-in-error`` means in R4. Raises
    :class:`FHIRExportRefusedError` for an absent or unrecognised reading status.
    """
    if not is_latest:
        return "entered-in-error"

    key = (reading_status or "").strip().lower()
    status = _READING_STATUS_TO_FHIR.get(key)
    if status is None:
        raise FHIRExportRefusedError(
            f"refusing FHIR export: unrecognised reading_status {reading_status!r} — "
            f"cannot assert a report status (known: {sorted(_READING_STATUS_TO_FHIR)})"
        )
    return status


def build_device(model_version: str, model_checksum: str | None = None) -> dict[str, Any]:
    """FHIR Device for the algorithm that produced a result (step 10).

    Every ``results_index`` row already carries ``model_version`` and ``model_checksum``
    as NOT NULL, so the raw material for AI attribution was always there — it just was
    never modelled. The checksum is the identifier: a version string can be reused across
    rebuilds, a content hash cannot, which is what makes a finding traceable to the exact
    artefact that produced it.
    """
    version = (model_version or "").strip()
    checksum = (model_checksum or "").strip()
    device: dict[str, Any] = {
        "resourceType": "Device",
        "status": "active",
        "deviceName": [
            {"name": f"MRCV {version}" if version else "MRCV", "type": "manufacturer-name"}
        ],
        "type": {"text": "AI image analysis software"},
    }
    if version:
        device["version"] = [{"value": version}]
    ident = identifier(MODEL_CHECKSUM_IDENTIFIER_SYSTEM, checksum)
    if ident:
        device["identifier"] = [ident]
    return device


def build_provenance(
    target_full_url: str, device_full_url: str, recorded: datetime | None = None
) -> dict[str, Any]:
    """FHIR Provenance attributing a report to the algorithm that produced it.

    This is the R4 mechanism for saying "an algorithm derived this, not a person". Without
    it an AI-generated conclusion is indistinguishable from a radiologist's, which is the
    distinction that matters most in an AI-assisted report.
    """
    return {
        "resourceType": "Provenance",
        "target": [{"reference": target_full_url}],
        "recorded": to_fhir_instant(recorded or utcnow()),
        "activity": {
            "coding": [{"system": DATA_OPERATION_SYSTEM, "code": "CREATE", "display": "create"}]
        },
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": PROVENANCE_PARTICIPANT_TYPE_SYSTEM,
                            "code": "assembler",
                            "display": "Assembler",
                        }
                    ]
                },
                "who": {"reference": device_full_url},
            }
        ],
    }


def _device_request(device: dict[str, Any]) -> dict[str, str]:
    """Conditional-update request for a Device, so one model build is one resource.

    Falls back to POST when the checksum is unknown — without an identifier there is
    nothing to match on, and a conditional update with no criteria would replace an
    arbitrary Device.
    """
    idents = device.get("identifier") or []
    if idents:
        system = idents[0].get("system")
        value = idents[0].get("value")
        if system and value:
            return {"method": "PUT", "url": f"Device?identifier={system}|{value}"}
    return {"method": "POST", "url": "Device"}


class FHIRClient:
    """Client for exporting results as FHIR resources."""

    def __init__(self, server_url: str | None = None):
        settings = get_settings()
        self._server_url = (server_url or settings.fhir_server_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._client.aclose()

    async def create_diagnostic_report(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any] | None = None,
        reading_status: str | None = None,
        is_latest: bool = True,
        accession_number: str | None = None,
        patient_mrn: str | None = None,
    ) -> dict[str, Any]:
        """Create a FHIR DiagnosticReport from an AI result.

        ``reading_status`` is the owning study's ``studies.reading_status`` and is
        required: it decides DiagnosticReport.status. Omitting it refuses the export
        rather than guessing (see :func:`resolve_report_status`).
        """
        status = resolve_report_status(reading_status, is_latest)

        settings = get_settings()

        # identifier[] identifies *this report*. The study UID always qualifies; the
        # accession number is added only when the deployment has declared which
        # namespace issued it — an unqualified identifier cannot be matched against a
        # foreign system, and emitting one invites a false match (see fhir_terminology).
        identifiers = [dicom_uid_identifier(study_instance_uid)]
        accession_identifier = identifier(
            settings.fhir_accession_identifier_system, accession_number or ""
        )
        if accession_identifier:
            identifiers.append(accession_identifier)
        elif accession_number:
            logger.warning(
                "fhir_accession_identifier_omitted",
                study_uid=study_instance_uid,
                reason="fhir_accession_identifier_system not configured",
            )

        report = {
            "resourceType": "DiagnosticReport",
            "status": status,
            "category": [
                {
                    "coding": [
                        {
                            "system": DIAGNOSTIC_SERVICE_SECTION_SYSTEM,
                            "code": "RAD",
                            "display": "Radiology",
                        }
                    ]
                }
            ],
            "code": {
                "coding": [
                    {
                        "system": LOINC_SYSTEM,
                        "code": "18748-4",
                        # Coding.display must be the display name *of the code*, not
                        # free text — it previously read "AI Analysis - {usecase}",
                        # which silently redefined what 18748-4 means. The use-case
                        # label belongs in code.text below.
                        "display": "Diagnostic imaging study",
                    }
                ],
                "text": f"AI Analysis: {usecase_name}",
            },
            # instant — guarded so a naive value can never be emitted (see the helper).
            "issued": to_fhir_instant(utcnow()),
            "conclusion": self._build_conclusion(result, usecase_name),
            "identifier": identifiers,
        }

        # subject: a *logical* reference, carrying the patient's identifier rather than a
        # server-side id. R4 Reference permits identifier-only, and it is the correct form
        # here: the only patient key this platform holds is the DICOM PatientID (an MRN in
        # the hospital's namespace), whereas "Patient/{id}" would assert a logical id on
        # the target server — which either dangles or, far worse, resolves to a different
        # patient and attaches these findings to the wrong chart.
        #
        # An identifier reference lets the receiving system resolve the patient itself,
        # with no query from us and nothing invented. It requires the MRN's namespace to
        # be known: without fhir_patient_identifier_system the identifier would be
        # unqualified and could false-match, so the subject is omitted and logged instead.
        # Note `display` is intentionally absent — the patient name is PHI and does not
        # belong in an outbound payload (FHIR_INTEGRATION_PLAN.md §4.2).
        mrn = (patient_mrn or (patient_info or {}).get("patient_id") or "").strip()
        subject_identifier = identifier(settings.fhir_patient_identifier_system, mrn)
        if subject_identifier:
            report["subject"] = {"type": "Patient", "identifier": subject_identifier}
        elif mrn:
            logger.warning(
                "fhir_subject_omitted",
                study_uid=study_instance_uid,
                reason="fhir_patient_identifier_system not configured",
            )

        # No Observation resources are emitted, by design. The previous implementation
        # looped over `measurements` selecting isinstance(value, (int, float)) and built
        # Observations with code.text only (no coding) and unit "unknown" (no UCUM). That
        # was wrong in both directions: for brain_mri it published technical metadata
        # (image_dimensions, candidate_slices) into a patient chart as clinical findings,
        # while for pet_ct every top-level measurements value is a dict/list, so SUVmax,
        # MTV and TLG — the clinically valuable numbers — were silently dropped.
        # A blanket loop cannot be made safe; export needs a per-plugin whitelist with
        # LOINC/UCUM codes. See FHIR_R4_COMPLIANCE_AUDIT.md steps 4-5.
        #
        # report["_observations"] is gone too: DiagnosticReport has no such element, and
        # "_"-prefixed names are reserved in FHIR JSON for primitive extensions, so it
        # made every payload fail validation. Those Observations were also never POSTed
        # and never referenced from DiagnosticReport.result[], so nothing is lost here
        # that previously worked.

        # ── Device + Provenance: AI attribution (step 10) ──────────────────────
        # Marks the conclusion as algorithm-derived rather than human-authored. Emitted as
        # a transaction Bundle because the three resources must land together or not at
        # all — a report whose Provenance failed to save would silently read as though a
        # person wrote it, which is the exact misattribution this exists to prevent.
        model_version = (result.get("model_version") or "").strip()
        if model_version:
            report_url = f"urn:uuid:{uuid.uuid4()}"
            device_url = f"urn:uuid:{uuid.uuid4()}"
            device = build_device(model_version, result.get("model_checksum"))
            bundle = {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "fullUrl": report_url,
                        "resource": report,
                        # Conditional update by the study UID identifier, so re-exporting a
                        # study updates the same logical report instead of duplicating it.
                        "request": {
                            "method": "PUT",
                            "url": (
                                "DiagnosticReport?identifier="
                                f"{DICOM_UID_SYSTEM}|urn:oid:{study_instance_uid}"
                            ),
                        },
                    },
                    {
                        "fullUrl": device_url,
                        "resource": device,
                        "request": _device_request(device),
                    },
                    {
                        "fullUrl": f"urn:uuid:{uuid.uuid4()}",
                        "resource": build_provenance(report_url, device_url),
                        "request": {"method": "POST", "url": "Provenance"},
                    },
                ],
            }
            return await self._submit(bundle, study_instance_uid, "")

        # No model version on the result (nothing to attribute) — send the bare report.
        return await self._submit(report, study_instance_uid, "/DiagnosticReport")

    async def _submit(
        self, payload: dict[str, Any], study_instance_uid: str, endpoint: str
    ) -> dict[str, Any]:
        """POST a payload when a server is configured; otherwise return it unsent.

        With no ``fhir_server_url`` the payload is returned as-is, which is what the
        ``/fhir`` endpoint uses to let an integrator inspect exactly what *would* be sent
        before any server is wired up.
        """
        if not self._server_url:
            return payload
        try:
            response = await self._client.post(
                f"{self._server_url}{endpoint}",
                json=payload,
                headers={"Content-Type": "application/fhir+json"},
            )
            if response.status_code in (200, 201):
                logger.info(
                    "fhir_payload_submitted",
                    study_uid=study_instance_uid,
                    resource_type=payload.get("resourceType"),
                )
                return response.json()
            logger.warning(
                "fhir_submit_failed",
                study_uid=study_instance_uid,
                resource_type=payload.get("resourceType"),
                status=response.status_code,
                body=response.text[:500],
            )
        except Exception as exc:
            logger.error("fhir_client_error", study_uid=study_instance_uid, error=str(exc))
        return payload

    # Result keys that are genuinely narrative and belong in a report conclusion. Anything
    # not listed here and not whitelisted in the plugin's fhir_map.yaml is left out.
    _NARRATIVE_KEYS = ("diagnosis", "impression", "findings", "processing_notes")

    def _build_conclusion(self, result: dict[str, Any], usecase_name: str = "") -> str:
        """Compose DiagnosticReport.conclusion from narrative text plus whitelisted values.

        Previously this dumped every ``summary`` and ``measurements`` key into the
        conclusion, which put ``Image Dimensions: [168, 168, 331]`` and
        ``Voxel Spacing Mm: [4.07, 4.07, 3.27]`` into the human-readable body of a
        radiology report. Structured Observations are whitelisted per plugin
        (fhir_map.yaml); the narrative must respect the same whitelist, or the technical
        metadata simply reappears as prose — a laxer version of the §3.5 problem.
        """
        summary = result.get("summary", {}) or {}
        parts: list[str] = []

        # 1. Narrative prose, in a stable order.
        for key in self._NARRATIVE_KEYS:
            value = summary.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

        # 2. The AI-authored report, where a plugin produced one (MedGemma / Gemini).
        ai_report = summary.get("ai_report")
        if isinstance(ai_report, dict):
            for key in ("findings", "impression"):
                value = ai_report.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())

        # 3. Whitelisted measurements only, rendered with their display name and UCUM
        #    unit. Reuses the plugin map so the narrative and the Observations can never
        #    disagree about what is clinical.
        try:
            from app.application.fhir_terminology import extract_values

            measured = []
            for item in extract_values(usecase_name, result):
                # UCUM "1" is dimensionless (SUV, ratios) — printing it would read
                # as "SUVmax: 12.86 1".
                unit = item.spec.unit or ""
                suffix = f" {unit}" if unit and unit != "1" else ""
                measured.append(f"{item.display}: {item.value}{suffix}")
        except Exception as exc:   # never let terminology loading break a report
            logger.warning("conclusion_measurements_skipped", error=str(exc))
            measured = []
        if measured:
            parts.append("Measurements:")
            parts.extend(f"  {line}" for line in measured)

        qa_flags = result.get("qa_flags", [])
        if qa_flags:
            parts.append(f"QA Flags: {', '.join(str(f) for f in qa_flags)}")

        return "\n".join(parts) if parts else "AI analysis completed."
