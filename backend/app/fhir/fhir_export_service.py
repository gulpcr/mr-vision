from __future__ import annotations

from typing import Any

import structlog

from app.config import get_settings
from app.fhir.client import FHIRClient

logger = structlog.get_logger(__name__)


class FHIRExportService:
    """High-level service for exporting results to FHIR."""

    async def export_result(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            )
            return {"status": "ok", "report": report}
        except Exception as e:
            logger.error("fhir_export_failed", error=str(e))
            return {"status": "error", "message": str(e)}
        finally:
            await client.close()
