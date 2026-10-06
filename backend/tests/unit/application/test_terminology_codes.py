"""AI-03: SR / SEG exports use standard coded concepts (SNOMED CT, DCM, UCUM)."""
from __future__ import annotations

import io

import pydicom
from pydicom.dataset import Dataset

from app.domain.terminology_codes import (
    ANATOMICAL_STRUCTURE,
    finding_codes,
    segment_codes,
    unit_for,
)
from app.services.dicom_export_service import _USECASE_LABELS, DICOMExportService


def test_every_segment_label_has_its_own_snomed_type():
    seen = {}
    for labels in _USECASE_LABELS.values():
        for label in labels.values():
            category, typ = segment_codes(label)
            assert category[1] == "SCT" and typ[1] == "SCT"
            assert typ[0] != "85756007", label           # no longer the generic "Tissue"
            seen[label] = typ
    assert seen["Liver"] == ("10200004", "SCT", "Liver")
    assert segment_codes("Left Kidney")[0] == ANATOMICAL_STRUCTURE
    assert len({t[0] for k, t in seen.items() if k not in ("Tumor Core", "Whole Tumor", "Enhancing Tumor",
                                                           "Lesion", "Brain Lesion", "Coronary Calcium")}) > 10


def test_finding_codes_follow_what_the_ai_asserted():
    assert finding_codes({"tumor_detected": True})[0][0] == "108369006"
    assert finding_codes({"anomaly_findings": [{"organ": "liver"}]})[0][0] == "263654008"
    assert finding_codes({"tumor_detected": False})[0][0] == "17621005"
    assert finding_codes({}) == []


def test_units_are_ucum():
    assert unit_for("lesion_volume_ml")[0] == "mL"
    assert unit_for("Max Diameter Mm")[0] == "mm"
    assert unit_for("suv_max")[0] == "{SUVbw}g/mL"
    assert unit_for("agatston_score") == ("1", "UCUM", "no units")


def test_sr_carries_coded_site_finding_and_units():
    svc = object.__new__(DICOMExportService)
    src = Dataset()
    src.StudyInstanceUID = "1.2.3"
    data = svc._build_sr(src, {"study_instance_uid": "1.2.3", "model_version": "1",
                               "summary": {"tumor_detected": True},
                               "measurements": {"lesion_volume_ml": 4.2}}, "brain_mri")
    ds = pydicom.dcmread(io.BytesIO(data))
    codes = [(i.ConceptNameCodeSequence[0].CodeValue, i.ConceptCodeSequence[0].CodeValue)
             for i in ds.ContentSequence if i.ValueType == "CODE"]
    assert ("363698007", "12738006") in codes      # finding site: brain
    assert ("121071", "108369006") in codes        # finding: neoplasm
    nums = [i for i in ds.ContentSequence if i.ValueType == "NUM"]
    assert nums[0].MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0].CodeValue == "mL"
