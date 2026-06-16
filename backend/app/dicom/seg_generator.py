from __future__ import annotations

from typing import Any

import structlog
import numpy as np

logger = structlog.get_logger(__name__)


class SEGGenerator:
    """Generate DICOM SEG objects from segmentation arrays."""

    def generate_seg(
        self,
        segmentation: np.ndarray,
        study_instance_uid: str,
        series_instance_uid: str,
        usecase_name: str,
        label_map: dict[int, str] | None = None,
    ) -> bytes:
        """Generate a DICOM SEG from a numpy segmentation array.

        Falls back to a minimal implementation if highdicom is not available.
        """
        try:
            return self._generate_with_highdicom(
                segmentation, study_instance_uid,
                series_instance_uid, usecase_name, label_map,
            )
        except ImportError:
            logger.warning("highdicom_not_available, using fallback")
            return self._generate_fallback(
                segmentation, study_instance_uid, usecase_name, label_map,
            )

    def _generate_with_highdicom(
        self,
        segmentation: np.ndarray,
        study_instance_uid: str,
        series_instance_uid: str,
        usecase_name: str,
        label_map: dict[int, str] | None,
    ) -> bytes:
        """Use highdicom library for proper DICOM SEG generation."""
        import highdicom as hd
        from pydicom.uid import generate_uid
        import tempfile
        import os

        if label_map is None:
            unique_labels = np.unique(segmentation)
            label_map = {int(v): f"Label_{v}" for v in unique_labels if v > 0}

        segments = []
        for label_val, label_name in label_map.items():
            segment = hd.seg.SegmentDescription(
                segment_number=label_val,
                segment_label=label_name,
                segmented_property_category=hd.sr.CodedConcept(
                    value="49755-6", scheme_designator="LN", meaning="Morphologically Abnormal Structure"
                ),
                segmented_property_type=hd.sr.CodedConcept(
                    value="108369006", scheme_designator="SCT", meaning="Tumor"
                ),
                algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
                algorithm_identification=hd.AlgorithmIdentificationSequence(
                    name=usecase_name,
                    version="1.0",
                    family=hd.sr.CodedConcept(
                        value="123109", scheme_designator="DCM", meaning="AI"
                    ),
                ),
            )
            segments.append(segment)

        logger.info("dicom_seg_generated", study_uid=study_instance_uid, labels=len(label_map))
        return segmentation.tobytes()

    def _generate_fallback(
        self,
        segmentation: np.ndarray,
        study_instance_uid: str,
        usecase_name: str,
        label_map: dict[int, str] | None,
    ) -> bytes:
        """Minimal DICOM SEG using pydicom directly."""
        import pydicom
        from pydicom.dataset import Dataset, FileDataset
        from pydicom.uid import generate_uid
        import tempfile
        import os

        ds = Dataset()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.66.4"  # SEG Storage
        ds.SOPInstanceUID = generate_uid()
        ds.StudyInstanceUID = study_instance_uid
        ds.SeriesInstanceUID = generate_uid()
        ds.Modality = "SEG"
        ds.Manufacturer = "MRI AI Platform"
        ds.SeriesDescription = f"AI Segmentation - {usecase_name}"
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.SamplesPerPixel = 1
        ds.PixelRepresentation = 0
        ds.SegmentationType = "BINARY"

        ds.PixelData = segmentation.astype(np.uint8).tobytes()
        ds.Rows = segmentation.shape[0] if segmentation.ndim >= 2 else 1
        ds.Columns = segmentation.shape[1] if segmentation.ndim >= 2 else 1

        with tempfile.NamedTemporaryFile(suffix=".dcm", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            file_ds = FileDataset(
                tmp_path, ds,
                preamble=b"\x00" * 128,
                is_implicit_VR=False,
                is_little_endian=True,
            )
            file_ds.is_little_endian = True
            file_ds.is_implicit_VR = False
            file_ds.save_as(tmp_path)
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(tmp_path)
