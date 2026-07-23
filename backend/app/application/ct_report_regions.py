from __future__ import annotations

"""Body-region metadata for the VLM report use-case family (CT + MRI).

The ``abdomen_ct`` two-pass MedGemma read (scan whole volume → flag → detailed read →
consolidated report) is body-part- and modality-agnostic; only the windows/sequences,
target pathologies, organ checklist and report headings change per region. Those live in
each plugin (``prompts.py`` ``REGION`` dict + ``inference_config.yaml``). This module
holds the SMALL slice of region metadata the shared orchestration needs — the
report-writer role label, whether tumour-marker correlation applies, and the report
document's EXAMINATION title / TECHNIQUE line — keyed by use-case name.

The family now covers both the CT plugins (``abdomen_ct`` + the ``ct_*`` siblings) and
the MRI plugins (``*_mri``), which share the identical MedGemma methodology (MRI renders a
per-volume percentile window instead of fixed HU windows — see each plugin's pipeline).

Single source of truth for the backend; the frontend mirrors the titles in
``ui/src/lib/ctReport.ts`` / ``AbdomenCtReport.tsx``.
"""

from typing import Any

# The VLM report family: use cases driven by the shared full-volume MedGemma scan hook in
# ``infrastructure/queue/tasks.py``. Membership gates the hook and the report UI. The name
# is kept as CT_REPORT_USECASES for backwards compatibility; it now also holds the MRI
# plugins, which use the same two-pass scan→flag→report methodology.
CT_REPORT_USECASES: set[str] = {
    # CT
    "abdomen_ct",
    "ct_brain",
    "ct_neck",
    "ct_chest",
    "ct_lumbar_spine",
    "ct_lower_limb",
    "ct_face",
    # MRI (same methodology; per-volume percentile render instead of HU windows)
    "brain_mri",
    "neck_mri",
    "chest_mri",
    "abdomen_mri",
    "lumbar_spine_mri",
    "lower_limb_mri",
}

# Per-use-case report metadata.
#   region_label      role framing for the report-writer ("... of {region_label} report")
#   examination       EXAMINATION heading of the rendered report document
#   technique         TECHNIQUE line
#   markers_enabled   whether serum tumour-marker correlation is offered in CONCLUSIONS.
#                     Only oncologic soft-tissue regions (abdomen/pelvis, neck) have
#                     site-specific serum markers; brain/chest/spine/limb do not, so the
#                     tumour-marker advice is suppressed there.
_REGIONS: dict[str, dict[str, Any]] = {
    "abdomen_ct": {
        "region_label": "an ABDOMEN/PELVIS CT",
        "examination": "CT SCAN OF THE ABDOMEN AND PELVIS",
        "technique": (
            "Axial CT images of the abdomen and pelvis were reviewed. Findings below are an "
            "AI-assisted read of soft-tissue-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": True,
    },
    "ct_brain": {
        "region_label": "a BRAIN (HEAD) CT",
        "examination": "CT SCAN OF THE BRAIN",
        "technique": (
            "Axial CT images of the brain were reviewed. Findings below are an AI-assisted read "
            "of brain-, blood- and bone-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "ct_neck": {
        "region_label": "a NECK CT",
        "examination": "CT SCAN OF THE NECK",
        "technique": (
            "Axial CT images of the neck were reviewed. Findings below are an AI-assisted read of "
            "soft-tissue-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": True,
    },
    "ct_chest": {
        "region_label": "a CHEST CT",
        "examination": "CT SCAN OF THE CHEST",
        "technique": (
            "Axial CT images of the chest were reviewed. Findings below are an AI-assisted read of "
            "lung- and mediastinal-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "ct_lumbar_spine": {
        "region_label": "a LUMBAR SPINE CT",
        "examination": "CT SCAN OF THE LUMBAR SPINE",
        "technique": (
            "Axial CT images of the lumbar spine were reviewed. Findings below are an AI-assisted "
            "read of bone- and soft-tissue-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "ct_lower_limb": {
        "region_label": "a LOWER LIMB CT",
        "examination": "CT SCAN OF THE LOWER LIMB",
        "technique": (
            "Axial CT images of the lower limb were reviewed. Findings below are an AI-assisted "
            "read of bone- and soft-tissue-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "ct_face": {
        "region_label": "a FACE (MAXILLOFACIAL) CT",
        "examination": "CT SCAN OF THE FACE (MAXILLOFACIAL)",
        "technique": (
            "Axial CT images of the facial skeleton were reviewed. Findings below are an "
            "AI-assisted read of bone- and soft-tissue-windowed axial levels sampled across the volume."
        ),
        "markers_enabled": False,
    },
    # ── MRI plugins (same two-pass methodology; per-volume percentile render) ──
    "brain_mri": {
        "region_label": "a BRAIN MRI",
        "examination": "MRI OF THE BRAIN",
        "technique": (
            "Multiplanar multisequence MR images of the brain were reviewed. Findings below are an "
            "AI-assisted read of axial images sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "neck_mri": {
        "region_label": "a NECK MRI",
        "examination": "MRI OF THE NECK",
        "technique": (
            "Multiplanar multisequence MR images of the neck were reviewed. Findings below are an "
            "AI-assisted read of axial images sampled across the volume."
        ),
        "markers_enabled": True,
    },
    "chest_mri": {
        "region_label": "a CHEST MRI",
        "examination": "MRI OF THE CHEST",
        "technique": (
            "Multiplanar multisequence MR images of the chest were reviewed. Findings below are an "
            "AI-assisted read of axial images sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "abdomen_mri": {
        "region_label": "an ABDOMEN/PELVIS MRI",
        "examination": "MRI OF THE ABDOMEN AND PELVIS",
        "technique": (
            "Multiplanar multisequence MR images of the abdomen and pelvis were reviewed. Findings "
            "below are an AI-assisted read of axial images sampled across the volume."
        ),
        "markers_enabled": True,
    },
    "lumbar_spine_mri": {
        "region_label": "a LUMBAR SPINE MRI",
        "examination": "MRI OF THE LUMBAR SPINE",
        "technique": (
            "Multiplanar multisequence MR images of the lumbar spine were reviewed. Findings below "
            "are an AI-assisted read of axial images sampled across the volume."
        ),
        "markers_enabled": False,
    },
    "lower_limb_mri": {
        "region_label": "a LOWER LIMB MRI",
        "examination": "MRI OF THE LOWER LIMB",
        "technique": (
            "Multiplanar multisequence MR images of the lower limb were reviewed. Findings below are "
            "an AI-assisted read of axial images sampled across the volume."
        ),
        "markers_enabled": False,
    },
}

_DEFAULT = _REGIONS["abdomen_ct"]


def is_ct_report_usecase(usecase_name: str) -> bool:
    return usecase_name in CT_REPORT_USECASES


def region_meta(usecase_name: str) -> dict[str, Any]:
    """Region metadata for a use case (falls back to the abdomen profile)."""
    return _REGIONS.get(usecase_name, _DEFAULT)
