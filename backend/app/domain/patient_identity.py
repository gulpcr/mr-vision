"""How a patient is identified on screen (pure, stdlib only).

With ``display_patient_names`` off (the default), every human-facing surface shows the
MRN — the DICOM PatientID — where it would otherwise show the patient's name.
"""
from __future__ import annotations


def displayed_patient_name(name: str | None, mrn: str | None, show_names: bool) -> str | None:
    """The value to show in a "patient name" slot: the name, or the MRN when names are
    hidden (None when there is no MRN, so callers fall back to their "unknown" text)."""
    if show_names:
        return name
    mrn = (mrn or "").strip()
    return mrn or None
