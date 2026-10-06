"""PRV-01: what reaches an AI worker is de-identified (HIPAA Safe Harbor, 164.514(b))."""
from __future__ import annotations

import asyncio
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from app.domain.deidentification import BLANKED_TAGS, REMOVED_TAGS, cap_age
from app.domain.models import Study
from app.infrastructure.orthanc.deidentify import (
    DeidentifyingPACSClient,
    deidentified_study,
    deidentify_dataset,
    pseudonym,
)

SALT = "unit-test-salt-0123456789"
SECRETS = [b"Doe^Jane", b"MRN-777", b"ACC-555", b"General Hospital", b"Dr^Who", b"19290101",
           b"1 Main Street", b"555-0100", b"SN-12345", b"Op^Erator"]


def _identified(path: str | None = None, z: int = 0) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = MRImageStorage, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = "1.2.3.4", "1.2.3.4.5"
    ds.PatientName, ds.PatientID, ds.AccessionNumber = "Doe^Jane", "MRN-777", "ACC-555"
    ds.PatientBirthDate, ds.PatientAge, ds.PatientSex = "19290101", "095Y", "F"
    ds.PatientAddress, ds.PatientTelephoneNumbers = "1 Main Street", "555-0100"
    ds.InstitutionName, ds.ReferringPhysicianName, ds.OperatorsName = "General Hospital", "Dr^Who", "Op^Erator"
    ds.DeviceSerialNumber, ds.PatientWeight = "SN-12345", 71.5
    ds.StudyDate, ds.SeriesDate, ds.AcquisitionDate = "20240615", "20240615", "20240616"
    ds.StudyTime, ds.AcquisitionTime = "101500", "003000"
    ds.AcquisitionDateTime = "20240616003000.000000"
    item = Dataset()
    item.CodeMeaning, item.OperatorsName = "nested", "Op^Erator"
    ds.ProcedureCodeSequence = Sequence([item])
    ds.add_new(0x00191010, "LO", "Doe^Jane private")  # private tag
    ds.Modality, ds.Rows, ds.Columns, ds.InstanceNumber = "MR", 4, 4, z
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.ImagePositionPatient, ds.ImageOrientationPatient = [0, 0, z], [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing, ds.SliceThickness = [1, 1], 1
    ds.PixelData = (np.arange(16, dtype=np.uint16) + z).tobytes()
    if path:
        ds.save_as(path, write_like_original=False)
    return ds


def test_identifiers_are_removed_blanked_or_pseudonymised(tmp_path):
    path = str(tmp_path / "a.dcm")
    original = _identified(path)
    ds = deidentify_dataset(pydicom.dcmread(path), SALT)
    for kw in REMOVED_TAGS:
        assert kw not in ds, kw
    for kw in BLANKED_TAGS:
        assert not ds.get(kw), kw
    assert ds.PatientID == pseudonym("MRN-777", SALT) != "MRN-777"
    assert ds.PatientIdentityRemoved == "YES"
    assert ds.ProcedureCodeSequence[0].OperatorsName == ""
    assert not any(e.tag.is_private for e in ds)
    ds.save_as(path, write_like_original=True)
    raw = open(path, "rb").read()
    for secret in SECRETS:
        assert secret not in raw, secret
    # Inference inputs are untouched.
    assert ds.PixelData == original.PixelData
    assert float(ds.PatientWeight) == 71.5 and ds.PatientSex == "F"
    assert ds.StudyInstanceUID == original.StudyInstanceUID


def test_dates_shift_to_a_year_anchor_keeping_intervals_and_times():
    ds = deidentify_dataset(_identified(), SALT)
    assert ds.StudyDate == "20240101" and ds.SeriesDate == "20240101"
    assert ds.AcquisitionDate == "20240102"                  # still one day after the study
    assert ds.AcquisitionDateTime.startswith("20240102003000")
    assert ds.StudyTime == "101500" and ds.AcquisitionTime == "003000"


def test_ages_over_89_are_grouped():
    assert deidentify_dataset(_identified(), SALT).PatientAge == "090Y"
    assert cap_age("045Y") == "045Y" and cap_age("011M") == "011M" and cap_age(None) is None


def test_pseudonym_is_stable_per_salt():
    assert pseudonym("MRN-1", SALT) == pseudonym("MRN-1", SALT)
    assert pseudonym("MRN-1", SALT) != pseudonym("MRN-2", SALT)
    assert pseudonym("MRN-1", SALT) != pseudonym("MRN-1", "another-salt-value-xx")


def test_pipeline_study_has_no_direct_identifiers():
    s = deidentified_study(Study(study_instance_uid="1.2", patient_id="MRN-777", patient_name="Doe^Jane",
                                 patient_age="095Y", accession_number="ACC-555",
                                 referring_physician="Dr Who", institution_name="General Hospital",
                                 modality="MR", study_description="MRI BRAIN"), SALT)
    assert s.patient_name is None and s.accession_number is None and s.referring_physician is None
    assert s.institution_name is None and s.study_date is None
    assert s.patient_id == pseudonym("MRN-777", SALT)
    assert s.patient_age == "090Y" and s.modality == "MR" and s.study_description == "MRI BRAIN"


class _FakePACS:
    """Writes identified DICOM, as Orthanc would serve it."""

    async def download_series_dicoms(self, study_uid, series_uid, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        paths = [os.path.join(output_dir, f"{i:06d}.dcm") for i in range(3)]
        for i, p in enumerate(paths):
            _identified(p, z=i)
        return paths

    async def get_study(self, uid):
        return {"00100010": {"vr": "PN", "Value": [{"Alphabetic": "Doe^Jane"}]},
                "0020000D": {"vr": "UI", "Value": ["1.2.3.4"]}}

    async def get_series_list(self, uid):
        return [{"0008103E": {"vr": "LO", "Value": ["T1"]}, "00100020": {"vr": "LO", "Value": ["MRN-777"]}}]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_wrapper_hands_pipelines_only_deidentified_files(tmp_path):
    client = DeidentifyingPACSClient(_FakePACS(), SALT)
    paths = _run(client.download_series_dicoms("1.2.3.4", "1.2.3.4.5", str(tmp_path / "s")))
    for p in paths:
        raw = open(p, "rb").read()
        assert not any(secret in raw for secret in SECRETS), p
        assert pydicom.dcmread(p).PatientIdentityRemoved == "YES"


def test_wrapper_scrubs_study_json_and_refuses_pacs_writes():
    client = DeidentifyingPACSClient(_FakePACS(), SALT)
    study = _run(client.get_study("1.2.3.4"))
    assert "00100010" not in study and "0020000D" in study
    assert "00100020" not in _run(client.get_series_list("1.2.3.4"))[0]
    for call in (client.upload_dicom_instance(b""), client.download_study_archive("1"),
                 client.delete_study_by_uid("1")):
        with pytest.raises(PermissionError):
            _run(call)


def test_nifti_conversion_runs_on_deidentified_files(tmp_path):
    pytest.importorskip("SimpleITK")
    client = DeidentifyingPACSClient(_FakePACS(), SALT)
    out = _run(client.download_series_as_nifti("1.2.3.4", "1.2.3.4.5", str(tmp_path / "v.nii.gz")))
    assert os.path.getsize(out) > 0
