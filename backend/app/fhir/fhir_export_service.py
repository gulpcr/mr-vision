from __future__ import annotations

from typing import Any

import structlog

from app.config import get_settings
from app.fhir.client import FHIRClient, FHIRExportRefusedError

logger = structlog.get_logger(__name__)


class FHIRExportService:
    """High-level service for exporting results to FHIR."""

    async def export_result(
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
        """Export one result as a DiagnosticReport.

        ``reading_status`` (the owning study's ``studies.reading_status``) drives
        DiagnosticReport.status and is required — an absent or unrecognised value
        yields ``{"status": "refused"}`` rather than a report claiming to be final.
        """
        settings = get_settings()
        if not settings.fhir_enabled:
            return {"status": "disabled", "message": "FHIR export is not enabled"}

        client = FHIRClient()
        try:
            report = await client.create_diagnostic_report(
                study_instance_uid=study_instance_uid,
                usecase_name=usecase_name,
                result=result,
                patient_info=patient_info,
                reading_status=reading_status,
                is_latest=is_latest,
                accession_number=accession_number,
                patient_mrn=patient_mrn,
            )
            return {"status": "ok", "report": report}
        except FHIRExportRefusedError as e:
            # Not an error condition — the safety gate declined to publish. Surfaced
            # distinctly so callers never treat a refusal as a transport failure to retry.
            logger.warning(
                "fhir_export_refused",
                study_uid=study_instance_uid,
                usecase=usecase_name,
                reading_status=reading_status,
                reason=str(e),
            )
            return {"status": "refused", "message": str(e)}
        except Exception as e:
            logger.error("fhir_export_failed", error=str(e))
            return {"status": "error", "message": str(e)}
        finally:
            await client.close()
