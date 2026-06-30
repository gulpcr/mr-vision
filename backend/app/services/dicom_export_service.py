"""DICOM SR / Segmentation export service — Phase 7.

Builds DICOM Basic Text SR (SOP 1.2.840.10008.5.1.4.1.1.88.11) from pipeline
result measurements and DICOM Segmentation Image (SOP 1.2.840.10008.5.1.4.1.1.66.4)
from the segmentation NIfTI artifact, then uploads both to Orthanc PACS.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import os
import tempfile
from typing import Any

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence as DCMSequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

import structlog

logger = structlog.get_logger(__name__)

# Label names per use-case for DICOM Seg segment descriptions
_USECASE_LABELS: dict[str, dict[int, str]] = {
    "brain_mri": {1: "Tumor Core", 2: "Whole Tumor", 3: "Enhancing Tumor"},
    "spine_mri": {1: "Cervical Spine", 2: "Thoracic Spine", 3: "Lumbar Spine", 4: "Sacrum"},
    "chest_mri": {1: "Lung Left", 2: "Lung Right", 3: "Heart", 4: "Mediastinum"},
    "abdomen_mri": {1: "Liver", 2: "Spleen", 3: "Left Kidney", 4: "Right Kidney", 5: "Pancreas"},
    "pet_ct": {1: "Lesion"},
    "pet_ct_brain": {1: "Brain Lesion"},
    "coronary_cta": {1: "Coronary Calcium"},
}

_SEG_ARTIFACT_NAMES = (
    "segmentation.nii.gz",
    "lesion_mask.nii.gz",
    "lesion_mask",
    "seg.nii.gz",
)


class DICOMExportService:
    """Builds DICOM SR / Seg objects from pipeline results and uploads to Orthanc."""

    MANUFACTURER = "MRI AI Platform"
    SERIES_NUMBER_SR = 9901
    SERIES_NUMBER_SEG = 9902

    def __init__(self) -> None:
        from app.infrastructure.orthanc.client import OrthancPACSClient
        from app.infrastructure.storage.client import MinIOArtifactStore

        self._pacs = OrthancPACSClient()
        self._store = MinIOArtifactStore()

    async def export_result(
        self,
        study_instance_uid: str,
        usecase_name: str,
        result_data: dict[str, Any],
        export_sr: bool = True,
        export_seg: bool = False,
    ) -> dict[str, str]:
        """Export result to Orthanc as DICOM SR and/or Seg. Returns dict of Orthanc IDs."""
        exported: dict[str, str] = {}

        source_ds = await self._fetch_source_instance(study_instance_uid)

        if export_sr:
            try:
                sr_bytes = await asyncio.get_event_loop().run_in_executor(
                    None, self._build_sr, source_ds, result_data, usecase_name
                )
                orthanc_id = await self._pacs.upload_dicom_instance(sr_bytes)
                exported["sr_orthanc_id"] = orthanc_id
                logger.info("dicom_sr_uploaded", study_uid=study_instance_uid, orthanc_id=orthanc_id)
            except Exception as exc:
                logger.warning("dicom_sr_export_failed", study_uid=study_instance_uid, error=str(exc))

        if export_seg:
            try:
                seg_nifti_bytes = await self._load_seg_nifti(study_instance_uid, usecase_name)
                if seg_nifti_bytes is not None:
                    seg_dcm_bytes = await asyncio.get_event_loop().run_in_executor(
                        None, self._build_seg, source_ds, seg_nifti_bytes, usecase_name
                    )
                    orthanc_id = await self._pacs.upload_dicom_instance(seg_dcm_bytes)
                    exported["seg_orthanc_id"] = orthanc_id
                    logger.info("dicom_seg_uploaded", study_uid=study_instance_uid, orthanc_id=orthanc_id)
                else:
                    logger.warning(
                        "dicom_seg_nifti_not_found",
                        study_uid=study_instance_uid,
                        usecase=usecase_name,
                    )
            except Exception as exc:
                logger.warning("dicom_seg_export_failed", study_uid=study_instance_uid, error=str(exc))

        await self._pacs.close()
        return exported

    # ── Data loaders ──────────────────────────────────────────────────────────

    async def _fetch_source_instance(self, study_instance_uid: str) -> pydicom.Dataset:
        """Download one DICOM instance from the study for patient/study context tags."""
        orthanc_study_id = await self._pacs.get_orthanc_study_id(study_instance_uid)
        resp = await self._pacs._client.get(f"/studies/{orthanc_study_id}")
        resp.raise_for_status()
        study_info = resp.json()

        series_ids = study_info.get("Series", [])
        if not series_ids:
            raise ValueError(f"No series in study {study_instance_uid}")

        series_resp = await self._pacs._client.get(f"/series/{series_ids[0]}")
        series_resp.raise_for_status()
        instances = series_resp.json().get("Instances", [])
        if not instances:
            raise ValueError(f"No instances in first series of study {study_instance_uid}")

        dcm_resp = await self._pacs._client.get(
            f"/instances/{instances[0]}/file",
            headers={"Accept": "application/dicom"},
        )
        dcm_resp.raise_for_status()
        return pydicom.dcmread(io.BytesIO(dcm_resp.content), force=True)

    async def _load_seg_nifti(self, study_instance_uid: str, usecase_name: str) -> bytes | None:
        """Try common artifact filenames to locate the segmentation NIfTI in MinIO."""
        for name in _SEG_ARTIFACT_NAMES:
            key = f"{study_instance_uid}/{usecase_name}/{name}"
            try:
                data = await self._store.get(key)
                if data:
                    return data
            except Exception:
                continue
        return None

    # ── SR builder (pydicom, Basic Text SR) ──────────────────────────────────

    def _build_sr(
        self,
        source_ds: pydicom.Dataset,
        result_data: dict[str, Any],
        usecase_name: str,
    ) -> bytes:
        """Build a DICOM Basic Text SR encoding all measurements and AI summary fields."""
        now = datetime.datetime.utcnow()
        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M%S.%f")
        sop_uid = generate_uid()

        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.11"
        file_meta.MediaStorageSOPInstanceUID = sop_uid
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
        ds.is_implicit_VR = False
        ds.is_little_endian = True

        # Copy patient / study attributes from source DICOM
        for tag in (
            "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
            "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber",
            "StudyDescription", "ReferringPhysicianName", "InstitutionName",
        ):
            if hasattr(source_ds, tag):
                setattr(ds, tag, getattr(source_ds, tag))

        if not getattr(ds, "StudyInstanceUID", None):
            ds.StudyInstanceUID = result_data.get("study_instance_uid", generate_uid())

        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.11"
        ds.SOPInstanceUID = sop_uid
        ds.Modality = "SR"
        ds.Manufacturer = self.MANUFACTURER
        ds.ManufacturerModelName = "MRI AI Platform"
        ds.SoftwareVersions = "1.0"
        ds.ContentDate = date_str
        ds.ContentTime = time_str
        ds.SeriesDate = date_str
        ds.SeriesTime = time_str
        ds.SeriesInstanceUID = generate_uid()
        ds.SeriesNumber = self.SERIES_NUMBER_SR
        ds.InstanceNumber = 1
        ds.CompletionFlag = "COMPLETE"
        ds.VerificationFlag = "UNVERIFIED"
        ds.PreliminaryFlag = "FINAL"
        ds.ValueType = "CONTAINER"
        ds.ContinuityOfContent = "SEPARATE"
        ds.ConceptNameCodeSequence = self._code_seq("126000", "DCM", "Imaging Measurement Report")

        items: list[Dataset] = []

        # Observer / device context
        items.append(self._code_item("121005", "DCM", "Observer Type", "121007", "DCM", "Device"))
        items.append(self._text_item("121012", "DCM", "Device Observer UID", generate_uid()))
        items.append(self._text_item("121013", "DCM", "Device Observer Name", self.MANUFACTURER))

        # Procedure / study info
        items.append(self._text_item(
            "121106", "DCM", "Procedure Reported",
            usecase_name.replace("_", " ").title(),
        ))
        items.append(self._text_item(
            "121401", "DCM", "Study Instance UID",
            result_data.get("study_instance_uid", ""),
        ))
        model_version = result_data.get("model_version", "")
        if model_version:
            items.append(self._text_item("111003", "DCM", "Algorithm Version", str(model_version)))

        # Measurements
        measurements = result_data.get("measurements", {})
        for key, value in (measurements or {}).items():
            self._flatten_value(items, key.replace("_", " ").title(), value)

        # QA flags
        qa_flags = result_data.get("qa_flags", [])
        if qa_flags:
            flags_str = "; ".join(f.value if hasattr(f, "value") else str(f) for f in qa_flags)
            items.append(self._text_item("111505", "DCM", "QA Flags", flags_str))

        # LLM summary fields
        summary = result_data.get("summary", {})

        clinical_ctx = summary.get("clinical_context", {})
        if isinstance(clinical_ctx, dict):
            if clinical_ctx.get("risk_level"):
                items.append(self._text_item("111017", "DCM", "AI Risk Level", str(clinical_ctx["risk_level"])))
            if clinical_ctx.get("impression"):
                items.append(self._text_item("121106", "DCM", "AI Clinical Impression", str(clinical_ctx["impression"])[:10240]))
            recs = clinical_ctx.get("recommendations", [])
            if recs and isinstance(recs, list):
                items.append(self._text_item("121109", "DCM", "AI Recommendations", "; ".join(str(r) for r in recs[:5])[:10240]))

        lng = summary.get("longitudinal_analysis", {})
        if isinstance(lng, dict) and lng.get("trend"):
            items.append(self._text_item("121401", "DCM", "Longitudinal Trend", str(lng["trend"])))
            if lng.get("response_category"):
                items.append(self._text_item("121401", "DCM", "Response Category", str(lng["response_category"])))

        inference_method = summary.get("inference_method", "")
        if inference_method:
            items.append(self._text_item("111003", "DCM", "Inference Method", str(inference_method)))

        ds.ContentSequence = DCMSequence(items)

        buf = io.BytesIO()
        pydicom.dcmwrite(buf, ds)
        return buf.getvalue()

    # ── Seg builder (highdicom) ───────────────────────────────────────────────

    def _build_seg(
        self,
        source_ds: pydicom.Dataset,
        seg_nifti_bytes: bytes,
        usecase_name: str,
    ) -> bytes:
        """Build a DICOM Segmentation Image from the pipeline's NIfTI label map."""
        import highdicom as hd
        import nibabel as nib
        from highdicom.seg import (
            SegmentDescription,
            SegmentAlgorithmTypeValues,
            Segmentation,
            SegmentationTypeValues,
        )
        from highdicom.sr import CodedConcept

        # nibabel requires a real file for .nii.gz
        with tempfile.NamedTemporaryFile(suffix=".nii.gz", delete=False) as tmp:
            tmp.write(seg_nifti_bytes)
            tmp_path = tmp.name
        try:
            nib_img = nib.load(tmp_path)
        finally:
            os.unlink(tmp_path)

        seg_array = np.asarray(nib_img.dataobj, dtype=np.uint8)  # (X, Y, Z)
        affine = nib_img.affine

        label_map = _USECASE_LABELS.get(usecase_name, {1: "Segment 1"})

        segment_descriptions = [
            SegmentDescription(
                segment_number=label_id,
                segment_label=label_name,
                segmented_property_category=CodedConcept("85756007", "SCT", "Tissue"),
                segmented_property_type=CodedConcept("85756007", "SCT", label_name),
                algorithm_type=SegmentAlgorithmTypeValues.AUTOMATIC,
                algorithm_identification=hd.AlgorithmIdentificationSequence(
                    name=self.MANUFACTURER,
                    family=CodedConcept("113091", "DCM", "Artificial Intelligence"),
                    version="1.0",
                ),
            )
            for label_id, label_name in sorted(label_map.items())
        ]

        # NIfTI (X, Y, Z) → DICOM frame order (Z, Y, X) = (frames, rows, cols)
        seg_frames = seg_array.transpose(2, 1, 0).copy()

        # Derive per-frame geometry from NIfTI affine (RAS → DICOM LPS)
        ras_to_lps = np.diag([-1.0, -1.0, 1.0, 1.0])
        lps_aff = ras_to_lps @ affine
        vox_spacing = np.sqrt((lps_aff[:3, :3] ** 2).sum(axis=0))
        iop = list(lps_aff[:3, 0] / vox_spacing[0]) + list(lps_aff[:3, 1] / vox_spacing[1])
        num_frames = seg_frames.shape[0]

        extra_kwargs: dict[str, Any] = {}
        try:
            plane_positions = [
                hd.PlanePositionPatient(
                    image_position=[
                        float(v) for v in
                        lps_aff[:3, :3] @ np.array([0.0, 0.0, float(z)]) + lps_aff[:3, 3]
                    ]
                )
                for z in range(num_frames)
            ]
            extra_kwargs["plane_positions"] = plane_positions
            extra_kwargs["plane_orientation"] = hd.PlaneOrientationPatient(
                image_orientation=[float(v) for v in iop]
            )
            extra_kwargs["pixel_measures"] = hd.PixelMeasures(
                pixel_spacing=[float(vox_spacing[1]), float(vox_spacing[0])],
                slice_thickness=float(vox_spacing[2]),
            )
        except AttributeError:
            # Older highdicom — fall back to source_images geometry
            pass

        seg_obj = Segmentation(
            source_images=[source_ds],
            pixel_array=seg_frames,
            segmentation_type=SegmentationTypeValues.BINARY,
            segment_descriptions=segment_descriptions,
            series_instance_uid=generate_uid(),
            series_number=self.SERIES_NUMBER_SEG,
            sop_instance_uid=generate_uid(),
            instance_number=1,
            manufacturer=self.MANUFACTURER,
            manufacturer_model_name="MRI AI Platform",
            software_versions=("1.0",),
            device_serial_number="MRI-AI-001",
            content_description=f"AI segmentation — {usecase_name}",
            content_creator_name="MRI AI Platform",
            **extra_kwargs,
        )

        buf = io.BytesIO()
        seg_obj.save_as(buf)
        return buf.getvalue()

    # ── pydicom content-item helpers ──────────────────────────────────────────

    def _code_seq(self, value: str, scheme: str, meaning: str) -> DCMSequence:
        item = Dataset()
        item.CodeValue = value
        item.CodingSchemeDesignator = scheme
        item.CodeMeaning = meaning
        return DCMSequence([item])

    def _code_item(
        self,
        name_val: str, name_scheme: str, name_meaning: str,
        concept_val: str, concept_scheme: str, concept_meaning: str,
    ) -> Dataset:
        item = Dataset()
        item.RelationshipType = "CONTAINS"
        item.ValueType = "CODE"
        item.ConceptNameCodeSequence = self._code_seq(name_val, name_scheme, name_meaning)
        item.ConceptCodeSequence = self._code_seq(concept_val, concept_scheme, concept_meaning)
        return item

    def _text_item(self, code_val: str, scheme: str, meaning: str, text: str) -> Dataset:
        item = Dataset()
        item.RelationshipType = "CONTAINS"
        item.ValueType = "TEXT"
        item.ConceptNameCodeSequence = self._code_seq(code_val, scheme, meaning)
        item.TextValue = str(text)[:10240]
        return item

    def _num_item(self, label: str, value: float) -> Dataset:
        item = Dataset()
        item.RelationshipType = "CONTAINS"
        item.ValueType = "NUM"
        item.ConceptNameCodeSequence = self._code_seq("112039", "DCM", label[:63])
        measured = Dataset()
        measured.NumericValue = pydicom.valuerep.DSfloat(value)
        measured.FloatingPointValue = value
        measured.MeasurementUnitsCodeSequence = self._code_seq("1", "UCUM", "no units")
        item.MeasuredValueSequence = DCMSequence([measured])
        return item

    def _flatten_value(self, items: list[Dataset], label: str, value: Any, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(value, bool):
            items.append(self._text_item("112039", "DCM", label[:63], str(value)))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            items.append(self._num_item(label, float(value)))
        elif isinstance(value, str):
            items.append(self._text_item("112039", "DCM", label[:63], value))
        elif isinstance(value, dict):
            for sub_k, sub_v in value.items():
                self._flatten_value(items, f"{label} — {sub_k.replace('_', ' ').title()}", sub_v, depth + 1)
        elif isinstance(value, (list, tuple)) and len(value) <= 20:
            for i, v in enumerate(value):
                self._flatten_value(items, f"{label}[{i}]", v, depth + 1)
