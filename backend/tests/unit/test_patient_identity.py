"""MRN in place of patient names on screen (DISPLAY_PATIENT_NAMES off by default)."""
from __future__ import annotations

from types import SimpleNamespace

from app.domain.patient_identity import displayed_patient_name


def test_mrn_replaces_the_name_unless_names_are_shown():
    assert displayed_patient_name("DOE^JOHN", "TM-017", show_names=False) == "TM-017"
    assert displayed_patient_name("DOE^JOHN", "  ", show_names=False) is None
    assert displayed_patient_name("DOE^JOHN", "TM-017", show_names=True) == "DOE^JOHN"


def test_dicomweb_answers_carry_the_mrn_in_patient_name(monkeypatch):
    from app.interface.api import dicomweb

    monkeypatch.setattr(dicomweb, "get_settings", lambda: SimpleNamespace(display_patient_names=False))
    datasets = [{
        "00100010": {"vr": "PN", "Value": [{"Alphabetic": "DOE^JOHN"}]},
        "00100020": {"vr": "LO", "Value": ["TM-017"]},
        "00101001": {"vr": "PN", "Value": [{"Alphabetic": "ROE^JANE"}]},
        "0040A730": {"vr": "SQ", "Value": [{  # nested item naming the patient too
            "00100010": {"vr": "PN", "Value": [{"Alphabetic": "DOE^JOHN"}]},
        }]},
    }]
    dicomweb._mask_patient_names(datasets)
    ds = datasets[0]
    assert ds["00100010"] == {"vr": "PN", "Value": [{"Alphabetic": "TM-017"}]}
    assert "00101001" not in ds
    assert ds["0040A730"]["Value"][0]["00100010"] == {"vr": "PN"}  # no MRN there → empty


def test_study_search_cannot_filter_by_name_while_names_are_hidden(monkeypatch):
    from app.interface.api import dicomweb

    request = SimpleNamespace(query_params={"PatientName": "*DOE*", "00100010": "DOE", "limit": "25"})
    monkeypatch.setattr(dicomweb, "get_settings", lambda: SimpleNamespace(display_patient_names=False))
    assert dicomweb._query_params(request) == {"limit": "25"}
    monkeypatch.setattr(dicomweb, "get_settings", lambda: SimpleNamespace(display_patient_names=True))
    assert dicomweb._query_params(request)["PatientName"] == "*DOE*"
