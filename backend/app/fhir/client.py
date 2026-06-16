from __future__ import annotations

from typing import Any

import structlog
import httpx

from app.config import get_settings

logger = structlog.get_logger(__name__)


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
    ) -> dict[str, Any]:
        """Create a FHIR DiagnosticReport from AI result."""
        from datetime import datetime

        report = {
            "resourceType": "DiagnosticReport",
            "status": "final",
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                            "code": "RAD",
                            "display": "Radiology",
                        }
                    ]
                }
            ],
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "18748-4",
                        "display": f"AI Analysis - {usecase_name}",
                    }
                ],
                "text": f"AI Analysis: {usecase_name}",
            },
            "issued": datetime.utcnow().isoformat() + "Z",
            "conclusion": self._build_conclusion(result),
            "identifier": [
                {
                    "system": "urn:dicom:uid",
                    "value": f"urn:oid:{study_instance_uid}",
                }
            ],
        }

        if patient_info and patient_info.get("patient_id"):
            report["subject"] = {
                "reference": f"Patient/{patient_info['patient_id']}",
                "display": patient_info.get("patient_name", ""),
            }

        # Add observations for measurements
        observations = []
        for key, value in result.get("measurements", {}).items():
            if isinstance(value, (int, float)):
                obs = {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {
                        "text": key.replace("_", " ").title(),
                    },
                    "valueQuantity": {
                        "value": value,
                        "unit": "unknown",
                    },
                }
                observations.append(obs)

        report["_observations"] = observations

        if self._server_url:
            try:
                response = await self._client.post(
                    f"{self._server_url}/DiagnosticReport",
                    json=report,
                    headers={"Content-Type": "application/fhir+json"},
                )
                if response.status_code in (200, 201):
                    logger.info("fhir_report_created", study_uid=study_instance_uid)
                    return response.json()
                else:
                    logger.warning(
                        "fhir_report_failed",
                        status=response.status_code,
                        body=response.text[:500],
                    )
            except Exception as e:
                logger.error("fhir_client_error", error=str(e))

        return report

    def _build_conclusion(self, result: dict[str, Any]) -> str:
        parts = []
        summary = result.get("summary", {})
        for key, value in summary.items():
            parts.append(f"{key.replace('_', ' ').title()}: {value}")

        measurements = result.get("measurements", {})
        if measurements:
            parts.append("Measurements:")
            for key, value in measurements.items():
                parts.append(f"  {key.replace('_', ' ').title()}: {value}")

        qa_flags = result.get("qa_flags", [])
        if qa_flags:
            parts.append(f"QA Flags: {', '.join(str(f) for f in qa_flags)}")

        return "\n".join(parts) if parts else "AI analysis completed."
