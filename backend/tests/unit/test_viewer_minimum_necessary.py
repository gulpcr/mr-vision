"""PRV-03: the viewer receives only the DICOM attributes it needs."""
from __future__ import annotations

import io
import re
from types import SimpleNamespace

import pydicom

from app.domain.viewer_minimum_necessary import (
    SERIES_QIDO_KEEP,
    STUDY_QIDO_KEEP,
    VIEWER_EXCLUDED_TAGS,
)
from tests.unit.infrastructure.test_patient_name_mask import _instance


def _settings(minimise: bool, show_names: bool = False):
    return lambda: SimpleNamespace(display_patient_names=show_names, viewer_minimum_necessary=minimise)


def _study() -> dict:
    return {
        "0020000D": {"vr": "UI", "Value": ["1.2.3"]},
        "00100010": {"vr": "PN", "Value": [{"Alphabetic": "DOE^JOHN"}]},
        "00100020": {"vr": "LO", "Value": ["TM-017"]},
        "00100030": {"vr": "DA", "Value": ["19700101"]},
        "00101040": {"vr": "LO", "Value": ["1 Main St"]},
        "00080090": {"vr": "PN", "Value": [{"Alphabetic": "REF^DOC"}]},
        "00081030": {"vr": "LO", "Value": ["MRI BRAIN"]},
    }


def test_tag_lists_are_well_formed():
    for tags in (STUDY_QIDO_KEEP, SERIES_QIDO_KEEP, VIEWER_EXCLUDED_TAGS):
        assert all(re.fullmatch(r"[0-9A-F]{8}", t) for t in tags)
    # Nothing the lists keep is also on the exclusion list.
    assert not (STUDY_QIDO_KEEP | SERIES_QIDO_KEEP) & VIEWER_EXCLUDED_TAGS


def test_study_list_is_allowlisted_and_masked(monkeypatch):
    from app.interface.api import dicomweb

    monkeypatch.setattr(dicomweb, "get_settings", _settings(True))
    [s] = dicomweb._viewer_view([_study()], STUDY_QIDO_KEEP)
    assert set(s) == {"0020000D", "00100010", "00100020", "00081030"}
    assert s["00100010"]["Value"][0]["Alphabetic"] == "TM-017"


def test_metadata_drops_demographics_keeps_the_rest(monkeypatch):
    from app.interface.api import dicomweb

    monkeypatch.setattr(dicomweb, "get_settings", _settings(True, show_names=True))
    data = [dict(_study(), **{"0040A730": {"vr": "SQ", "Value": [
        {"00100030": {"vr": "DA", "Value": ["19700101"]}, "00080100": {"vr": "SH", "Value": ["x"]}},
    ]}})]
    [s] = dicomweb._viewer_view(data)
    assert "00100030" not in s and "00101040" not in s
    assert "00080090" in s and s["00100010"]["Value"][0]["Alphabetic"] == "DOE^JOHN"
    assert s["0040A730"]["Value"][0] == {"00080100": {"vr": "SH", "Value": ["x"]}}


def test_off_means_unchanged(monkeypatch):
    from app.interface.api import dicomweb

    monkeypatch.setattr(dicomweb, "get_settings", _settings(False, show_names=True))
    assert not dicomweb._rewrites()
    assert dicomweb._viewer_view([_study()], STUDY_QIDO_KEEP) == [_study()]


def test_retrieved_file_loses_demographics_keeps_pixels():
    from app.infrastructure.dicomweb.patient_name_mask import mask_part10

    src = pydicom.dcmread(io.BytesIO(_instance()))
    src.PatientBirthDate, src.PatientAddress = "19700101", "1 Main St"
    buf = io.BytesIO()
    src.save_as(buf, write_like_original=False)
    out = pydicom.dcmread(io.BytesIO(mask_part10(buf.getvalue(), mask_names=False,
                                                 drop_tags=VIEWER_EXCLUDED_TAGS)))
    assert "PatientBirthDate" not in out and "PatientAddress" not in out
    assert "OtherPatientNames" not in out
    assert str(out.PatientName) == "Doe^Jane"  # names shown: untouched
    assert out.PixelData == src.PixelData
