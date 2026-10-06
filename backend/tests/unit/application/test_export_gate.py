"""AI-01: AI drafts are PRELIMINARY until signed; nothing with findings leaves before."""
from __future__ import annotations

import io

import pydicom
from pydicom.dataset import Dataset

from app.application.export_gate import preliminary_webhook_payload
from app.config import get_settings
from app.services.dicom_export_service import DICOMExportService

PAYLOAD = {
    "study_instance_uid": "1.2.3", "usecase_name": "brain_mri", "result_id": "r1",
    "qa_flags": ["motion"], "measurements": {"lesion_volume_ml": 12.5},
    "measurements.lesion_volume_ml": 12.5,
}


def test_result_ready_webhook_carries_no_findings_before_sign_off():
    sent = preliminary_webhook_payload(PAYLOAD)
    assert sent == {"study_instance_uid": "1.2.3", "usecase_name": "brain_mri",
                    "result_id": "r1", "report_status": "preliminary"}


def test_gate_off_keeps_the_legacy_payload(monkeypatch):
    monkeypatch.setattr(get_settings(), "require_signed_for_export", False)
    assert preliminary_webhook_payload(PAYLOAD) == PAYLOAD


def _sr(verification=None) -> pydicom.Dataset:
    svc = object.__new__(DICOMExportService)  # no PACS / store needed to build
    src = Dataset()
    src.PatientID, src.StudyInstanceUID = "MRN-1", "1.2.3"
    data = svc._build_sr(src, {"study_instance_uid": "1.2.3", "summary": {}, "measurements": {"x": 1.0},
                               "model_version": "1.0"}, "brain_mri", verification)
    return pydicom.dcmread(io.BytesIO(data))


def test_unsigned_sr_is_an_unverified_preliminary_draft():
    ds = _sr()
    assert ds.VerificationFlag == "UNVERIFIED" and ds.PreliminaryFlag == "PRELIMINARY"
    assert "VerifyingObserverSequence" not in ds


def test_signed_sr_is_verified_by_the_signer():
    ds = _sr({"signer_full_name": "Dr Jane Reader", "signed_at": "2026-10-02T09:15:30+00:00"})
    assert ds.VerificationFlag == "VERIFIED" and ds.PreliminaryFlag == "FINAL"
    obs = ds.VerifyingObserverSequence[0]
    assert str(obs.VerifyingObserverName) == "Dr Jane Reader"
    assert obs.VerificationDateTime.startswith("20261002091530")
