// VLM report use-case family — the shared MedGemma full-volume scan → report plugins.
// Covers the CT plugins (abdomen_ct + the ct_* siblings) AND the MRI plugins (*_mri),
// which share the identical two-pass methodology (MRI renders a per-volume percentile
// window instead of fixed HU windows). Mirrors backend app/application/ct_report_regions.py.
// The report button, report view and report document key off this list so a new plugin
// only needs an entry here on the frontend. (Const name kept for backwards compatibility.)

export const CT_REPORT_USECASES = [
  // CT
  "abdomen_ct",
  "ct_brain",
  "ct_neck",
  "ct_chest",
  "ct_lumbar_spine",
  "ct_lower_limb",
  "ct_face",
  // MRI
  "brain_mri",
  "neck_mri",
  "chest_mri",
  "abdomen_mri",
  "lumbar_spine_mri",
  "lower_limb_mri",
] as const;

export type CtReportUsecase = (typeof CT_REPORT_USECASES)[number];

export function isCtReportUsecase(name: string | null | undefined): boolean {
  return !!name && (CT_REPORT_USECASES as readonly string[]).includes(name);
}

// Per-region report document headings (EXAMINATION / TECHNIQUE). Falls back to abdomen.
export const CT_REGION_META: Record<string, { examination: string; technique: string }> = {
  abdomen_ct: {
    examination: "CT SCAN OF THE ABDOMEN AND PELVIS",
    technique:
      "Axial CT images of the abdomen and pelvis were reviewed. Findings below are an AI-assisted read of soft-tissue-windowed axial levels sampled across the volume.",
  },
  ct_brain: {
    examination: "CT SCAN OF THE BRAIN",
    technique:
      "Axial CT images of the brain were reviewed. Findings below are an AI-assisted read of brain-, blood- and bone-windowed axial levels sampled across the volume.",
  },
  ct_neck: {
    examination: "CT SCAN OF THE NECK",
    technique:
      "Axial CT images of the neck were reviewed. Findings below are an AI-assisted read of soft-tissue-windowed axial levels sampled across the volume.",
  },
  ct_chest: {
    examination: "CT SCAN OF THE CHEST",
    technique:
      "Axial CT images of the chest were reviewed. Findings below are an AI-assisted read of lung- and mediastinal-windowed axial levels sampled across the volume.",
  },
  ct_lumbar_spine: {
    examination: "CT SCAN OF THE LUMBAR SPINE",
    technique:
      "Axial CT images of the lumbar spine were reviewed. Findings below are an AI-assisted read of bone- and soft-tissue-windowed axial levels sampled across the volume.",
  },
  ct_lower_limb: {
    examination: "CT SCAN OF THE LOWER LIMB",
    technique:
      "Axial CT images of the lower limb were reviewed. Findings below are an AI-assisted read of bone- and soft-tissue-windowed axial levels sampled across the volume.",
  },
  ct_face: {
    examination: "CT SCAN OF THE FACE (MAXILLOFACIAL)",
    technique:
      "Axial CT images of the facial skeleton were reviewed. Findings below are an AI-assisted read of bone- and soft-tissue-windowed axial levels sampled across the volume.",
  },
  // MRI plugins (same methodology; per-volume percentile render).
  brain_mri: {
    examination: "MRI OF THE BRAIN",
    technique:
      "Multiplanar multisequence MR images of the brain were reviewed. Findings below are an AI-assisted read of axial images sampled across the volume.",
  },
  neck_mri: {
    examination: "MRI OF THE NECK",
    technique:
      "Multiplanar multisequence MR images of the neck were reviewed. Findings below are an AI-assisted read of axial images sampled across the volume.",
  },
  chest_mri: {
    examination: "MRI OF THE CHEST",
    technique:
      "Multiplanar multisequence MR images of the chest were reviewed. Findings below are an AI-assisted read of axial images sampled across the volume.",
  },
  abdomen_mri: {
    examination: "MRI OF THE ABDOMEN AND PELVIS",
    technique:
      "Multiplanar multisequence MR images of the abdomen and pelvis were reviewed. Findings below are an AI-assisted read of axial images sampled across the volume.",
  },
  lumbar_spine_mri: {
    examination: "MRI OF THE LUMBAR SPINE",
    technique:
      "Multiplanar multisequence MR images of the lumbar spine were reviewed. Findings below are an AI-assisted read of axial images sampled across the volume.",
  },
  lower_limb_mri: {
    examination: "MRI OF THE LOWER LIMB",
    technique:
      "Multiplanar multisequence MR images of the lower limb were reviewed. Findings below are an AI-assisted read of axial images sampled across the volume.",
  },
};

export function ctRegionMeta(usecase: string): { examination: string; technique: string } {
  return CT_REGION_META[usecase] || CT_REGION_META.abdomen_ct;
}
