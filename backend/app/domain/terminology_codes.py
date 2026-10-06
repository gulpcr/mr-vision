from __future__ import annotations

"""Coded concepts for DICOM SR / SEG exports (DICOM PS3.16; SNOMED CT "SCT", DCM, UCUM).

Pure data. Every SNOMED CT code below is a released concept id; RadLex identifiers are
added to RADLEX only after verification against the current RadLex release by a clinical
terminologist (an unverified code is worse than none). The SR builder emits a RadLex code
alongside the SNOMED one whenever an entry exists.
"""

Code = tuple[str, str, str]  # (code value, coding scheme designator, code meaning)

# ── Segmentation: category and type per segment label ───────────────────────
ANATOMICAL_STRUCTURE: Code = ("123037004", "SCT", "Anatomical Structure")
ABNORMAL_STRUCTURE: Code = ("49755003", "SCT", "Morphologically Abnormal Structure")

NEOPLASM: Code = ("108369006", "SCT", "Neoplasm")
LESION: Code = ("52988006", "SCT", "Lesion")

SEGMENT_TYPES: dict[str, tuple[Code, Code]] = {
    # label: (category, type)
    "Tumor Core": (ABNORMAL_STRUCTURE, NEOPLASM),
    "Whole Tumor": (ABNORMAL_STRUCTURE, NEOPLASM),
    "Enhancing Tumor": (ABNORMAL_STRUCTURE, NEOPLASM),
    "Cervical Spine": (ANATOMICAL_STRUCTURE, ("122494005", "SCT", "Cervical spine")),
    "Thoracic Spine": (ANATOMICAL_STRUCTURE, ("122495006", "SCT", "Thoracic spine")),
    "Lumbar Spine": (ANATOMICAL_STRUCTURE, ("122496007", "SCT", "Lumbar spine")),
    "Sacrum": (ANATOMICAL_STRUCTURE, ("54735007", "SCT", "Sacrum")),
    "Lung Left": (ANATOMICAL_STRUCTURE, ("44029006", "SCT", "Left lung")),
    "Lung Right": (ANATOMICAL_STRUCTURE, ("3341006", "SCT", "Right lung")),
    "Heart": (ANATOMICAL_STRUCTURE, ("80891009", "SCT", "Heart")),
    "Mediastinum": (ANATOMICAL_STRUCTURE, ("72410000", "SCT", "Mediastinum")),
    "Liver": (ANATOMICAL_STRUCTURE, ("10200004", "SCT", "Liver")),
    "Spleen": (ANATOMICAL_STRUCTURE, ("78961009", "SCT", "Spleen")),
    "Left Kidney": (ANATOMICAL_STRUCTURE, ("18639004", "SCT", "Left kidney")),
    "Right Kidney": (ANATOMICAL_STRUCTURE, ("9846003", "SCT", "Right kidney")),
    "Pancreas": (ANATOMICAL_STRUCTURE, ("15776009", "SCT", "Pancreas")),
    "Lesion": (ABNORMAL_STRUCTURE, LESION),
    "Brain Lesion": (ABNORMAL_STRUCTURE, LESION),
    "Coronary Calcium": (ABNORMAL_STRUCTURE, LESION),
}


def segment_codes(label: str) -> tuple[Code, Code]:
    """(category, type) for a segment label; unknown labels are coded as a lesion."""
    return SEGMENT_TYPES.get(label, (ABNORMAL_STRUCTURE, LESION))


# ── SR: finding site per use case, finding codes ─────────────────────────────
FINDING: Code = ("121071", "DCM", "Finding")
FINDING_SITE: Code = ("363698007", "SCT", "Finding Site")
NORMAL: Code = ("17621005", "SCT", "Normal")
ABNORMAL: Code = ("263654008", "SCT", "Abnormal")

BRAIN: Code = ("12738006", "SCT", "Brain")
THORAX: Code = ("51185008", "SCT", "Thorax")
ABDOMEN: Code = ("113345001", "SCT", "Abdomen")
NECK: Code = ("45048000", "SCT", "Neck")
LOWER_LIMB: Code = ("61685007", "SCT", "Lower limb")
BREAST: Code = ("76752008", "SCT", "Breast")
WHOLE_BODY: Code = ("38266002", "SCT", "Entire body")
LUMBAR_SPINE: Code = SEGMENT_TYPES["Lumbar Spine"][1]
HEART: Code = SEGMENT_TYPES["Heart"][1]

USECASE_SITES: dict[str, Code] = {
    "brain_mri": BRAIN, "ct_brain": BRAIN, "pet_ct_brain": BRAIN,
    "chest_mri": THORAX, "ct_chest": THORAX,
    "abdomen_mri": ABDOMEN, "abdomen_ct": ABDOMEN,
    "neck_mri": NECK, "ct_neck": NECK,
    "lumbar_spine_mri": LUMBAR_SPINE, "ct_lumbar_spine": LUMBAR_SPINE, "spine_mri": LUMBAR_SPINE,
    "lower_limb_mri": LOWER_LIMB, "ct_lower_limb": LOWER_LIMB,
    "mammography": BREAST,
    "coronary_cta": HEART,
    "pet_ct": WHOLE_BODY,
}

# Verified RadLex identifiers, keyed like the SNOMED concepts above (code meaning → RID).
# Intentionally empty until reviewed against the RadLex release in use.
RADLEX: dict[str, Code] = {}


def finding_codes(summary: dict) -> list[Code]:
    """Coded overall finding(s) from a result summary (conservative: only what the AI
    explicitly asserted)."""
    s = summary or {}
    if s.get("tumor_detected") is True:
        return [NEOPLASM]
    abnormal = bool(s.get("anomaly_findings")) or bool(s.get("anomaly_slices")) or bool(s.get("lesions"))
    if abnormal:
        return [ABNORMAL]
    if s.get("tumor_detected") is False or s.get("anomaly_findings") == [] or s.get("anomaly_slices") == []:
        return [NORMAL]
    return []


# ── UCUM units from measurement names ────────────────────────────────────────
UNIT_SUFFIXES: tuple[tuple[str, Code], ...] = (
    ("_ml", ("mL", "UCUM", "milliliter")),
    ("_cm3", ("cm3", "UCUM", "cubic centimeter")),
    ("_mm3", ("mm3", "UCUM", "cubic millimeter")),
    ("_mm", ("mm", "UCUM", "millimeter")),
    ("_cm", ("cm", "UCUM", "centimeter")),
    ("_hu", ("[hnsf'U]", "UCUM", "Hounsfield unit")),
    ("_pct", ("%", "UCUM", "percent")),
    ("_percent", ("%", "UCUM", "percent")),
    ("suv", ("{SUVbw}g/mL", "UCUM", "Standardized Uptake Value body weight")),
    ("_kg", ("kg", "UCUM", "kilogram")),
    ("_s", ("s", "UCUM", "second")),
)
NO_UNITS: Code = ("1", "UCUM", "no units")


def unit_for(name: str) -> Code:
    key = (name or "").strip().lower().replace(" ", "_")
    for suffix, code in UNIT_SUFFIXES:
        if (suffix == "suv" and "suv" in key) or key.endswith(suffix):
            return code
    return NO_UNITS
