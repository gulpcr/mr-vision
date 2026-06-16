from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class SRGenerator:
    """Generate DICOM Structured Reports from AI results."""

    def generate_sr(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any] | None = None,
    ) -> bytes:
        """Generate a DICOM SR dataset as bytes.

        Uses pydicom to create a minimal SR document.
        """
        import pydicom
        from pydicom.dataset import Dataset, FileDataset
        from pydicom.uid import generate_uid
        from pydicom.sequence import Sequence
        import tempfile
        import os
        from datetime import datetime

        ds = Dataset()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.33"  # Comprehensive SR
        ds.SOPInstanceUID = generate_uid()
        ds.StudyInstanceUID = study_instance_uid
        ds.SeriesInstanceUID = generate_uid()
        ds.Modality = "SR"
        ds.Manufacturer = "MRI AI Platform"
        ds.SeriesDescription = f"AI Report - {usecase_name}"

        now = datetime.now()
        ds.ContentDate = now.strftime("%Y%m%d")
        ds.ContentTime = now.strftime("%H%M%S.%f")
        ds.InstanceCreationDate = ds.ContentDate
        ds.InstanceCreationTime = ds.ContentTime

        if patient_info:
            ds.PatientName = patient_info.get("patient_name", "")
            ds.PatientID = patient_info.get("patient_id", "")

        # Content Sequence - root container
        root_container = Dataset()
        root_container.RelationshipType = "CONTAINS"
        root_container.ValueType = "CONTAINER"
        root_container.ConceptNameCodeSequence = self._make_code_seq(
            "126000", "DCM", "Imaging Report"
        )

        content_items = []

        # Add summary items
        summary = result.get("summary", {})
        for key, value in summary.items():
            item = Dataset()
            item.RelationshipType = "CONTAINS"
            item.ValueType = "TEXT"
            item.ConceptNameCodeSequence = self._make_code_seq(
                "121071", "DCM", key.replace("_", " ").title()
            )
            item.TextValue = str(value)
            content_items.append(item)

        # Add measurements
        measurements = result.get("measurements", {})
        for key, value in measurements.items():
            if isinstance(value, (int, float)):
                item = Dataset()
                item.RelationshipType = "CONTAINS"
                item.ValueType = "NUM"
                item.ConceptNameCodeSequence = self._make_code_seq(
                    "121206", "DCM", key.replace("_", " ").title()
                )
                measured_value = Dataset()
                measured_value.NumericValue = str(value)
                measured_value.MeasurementUnitsCodeSequence = self._make_code_seq(
                    "1", "UCUM", "no units"
                )
                item.MeasuredValueSequence = Sequence([measured_value])
                content_items.append(item)

        root_container.ContentSequence = Sequence(content_items)
        ds.ContentSequence = Sequence([root_container])

        ds.is_little_endian = True
        ds.is_implicit_VR = False

        # Write to bytes
        with tempfile.NamedTemporaryFile(suffix=".dcm", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            file_ds = FileDataset(
                tmp_path, ds,
                preamble=b"\x00" * 128,
                is_implicit_VR=False,
                is_little_endian=True,
            )
            file_ds.save_as(tmp_path)
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(tmp_path)

    def _make_code_seq(self, value: str, scheme: str, meaning: str) -> Any:
        from pydicom.sequence import Sequence
        from pydicom.dataset import Dataset

        code = Dataset()
        code.CodeValue = value
        code.CodingSchemeDesignator = scheme
        code.CodeMeaning = meaning
        return Sequence([code])
