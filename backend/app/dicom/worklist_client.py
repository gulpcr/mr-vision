from __future__ import annotations

from typing import Any

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class WorklistClient:
    """DICOM C-FIND SCU for querying Modality Worklist."""

    def __init__(self):
        settings = get_settings()
        self._host = settings.worklist_scp_host
        self._port = settings.worklist_scp_port

    async def query_worklist(
        self,
        scheduled_date: str | None = None,
        modality: str = "MR",
        ae_title: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query scheduled procedures from a Worklist SCP.

        Uses pynetdicom for DICOM C-FIND if available, otherwise returns empty.
        """
        if not self._host:
            logger.warning("worklist_scp_not_configured")
            return []

        try:
            from pynetdicom import AE, QueryRetrievePresentationContexts
            from pynetdicom.sop_class import ModalityWorklistInformationFind
            from pydicom.dataset import Dataset

            ae = AE(ae_title=ae_title or "MRI_AI_PLATFORM")
            ae.add_requested_context(ModalityWorklistInformationFind)

            ds = Dataset()
            ds.ScheduledProcedureStepSequence = [Dataset()]
            step = ds.ScheduledProcedureStepSequence[0]
            step.Modality = modality
            if scheduled_date:
                step.ScheduledProcedureStepStartDate = scheduled_date
            else:
                step.ScheduledProcedureStepStartDate = ""

            ds.PatientName = ""
            ds.PatientID = ""
            ds.StudyInstanceUID = ""
            ds.AccessionNumber = ""
            ds.RequestedProcedureDescription = ""

            results = []
            assoc = ae.associate(self._host, self._port)
            if assoc.is_established:
                responses = assoc.send_c_find(
                    ds, ModalityWorklistInformationFind
                )
                for status, identifier in responses:
                    if status and status.Status in (0xFF00, 0xFF01):
                        if identifier:
                            item = {
                                "patient_name": str(getattr(identifier, "PatientName", "")),
                                "patient_id": str(getattr(identifier, "PatientID", "")),
                                "study_instance_uid": str(getattr(identifier, "StudyInstanceUID", "")),
                                "accession_number": str(getattr(identifier, "AccessionNumber", "")),
                                "description": str(getattr(identifier, "RequestedProcedureDescription", "")),
                            }
                            sps = getattr(identifier, "ScheduledProcedureStepSequence", [])
                            if sps:
                                item["modality"] = str(getattr(sps[0], "Modality", ""))
                                item["scheduled_date"] = str(getattr(sps[0], "ScheduledProcedureStepStartDate", ""))
                                item["station_name"] = str(getattr(sps[0], "ScheduledStationName", ""))
                            results.append(item)
                assoc.release()
            else:
                logger.error("worklist_association_failed", host=self._host, port=self._port)

            return results

        except ImportError:
            logger.warning("pynetdicom_not_installed")
            return []
        except Exception as e:
            logger.error("worklist_query_failed", error=str(e))
            return []
