# PET/CT & PET-CT Implementation Prompt
# For Claude / AI Coding Agent

---

## CONTEXT

You are working inside an existing production medical imaging AI platform at:
`backend/app/`

The platform already supports Brain MRI, Spine MRI, Chest MRI, and Abdomen MRI
as use-case plugins. Each plugin lives under `backend/app/usecases/<name>/` and
consists of these files:

```
manifest.yaml            ← required, registered at startup
routing_rules.yaml       ← optional, auto-routes studies from PACS
pipeline.py              ← required, class Pipeline(BasePipeline)
outputs_schema.json      ← required, JSON Schema for result validation
ui_schema.json           ← optional, frontend rendering config
model/inference_config.yaml  ← required, model & inference hyperparameters
model/bundles/           ← created at runtime (model weights cache)
```

The base class every pipeline must inherit is:

```python
# backend/app/usecases/base.py
class BasePipeline(abc.ABC):
    @abc.abstractmethod
    def preprocess(self, study, series, working_dir, pacs, event_loop=None) -> dict
    @abc.abstractmethod
    def infer(self, preprocessed, working_dir) -> dict
    @abc.abstractmethod
    def postprocess(self, inference_output, working_dir) -> dict
```

The registry auto-discovers any folder in `usecases/` that has `manifest.yaml`
and imports the class named exactly `Pipeline` from `pipeline.py`.

---

## YOUR TASK

Implement **two new use-case plugins** inside `backend/app/usecases/`:

1. `pet_ct/`       — whole-body PET/CT oncology pipeline
2. `pet_ct_brain/` — brain-focused PET/CT (FDG/amyloid/tau) pipeline

Each plugin is self-contained. Do not modify any existing use cases or core
platform files. Add Python dependencies to `backend/requirements.txt` only.

---

## PLUGIN 1 — `pet_ct` (Whole-Body Oncology)

### Clinical purpose
Whole-body FDG-PET/CT for oncology staging, treatment response, and follow-up.
Detects hypermetabolic lesions, computes standardised uptake values (SUV),
metabolic tumour volume (MTV), and total lesion glycolysis (TLG). Correlates
with anatomical CT for lesion localisation.

---

### File: `backend/app/usecases/pet_ct/manifest.yaml`

```yaml
name: pet_ct
version: "1.0.0"
description: >
  Whole-body PET/CT oncology pipeline. Detects FDG-avid lesions, computes
  SUVmax, SUVmean, SUVpeak, metabolic tumour volume (MTV), and total lesion
  glycolysis (TLG). Produces lesion-level and whole-body summary metrics.
  Supports PERCIST 1.0 and Deauville score output.
supported_body_parts:
  - WHOLEBODY
  - CHEST
  - ABDOMEN
  - PELVIS
  - NECK
  - THORAX
required_sequences:
  - PET
  - CT
model_type: swin_unetr
enabled: true
```

---

### File: `backend/app/usecases/pet_ct/routing_rules.yaml`

```yaml
rules:
  - body_parts:
      - WHOLEBODY
      - CHEST
      - ABDOMEN
      - PELVIS
    study_description_patterns:
      - "(?i)pet"
      - "(?i)pet.*ct"
      - "(?i)fdg"
      - "(?i)positron"
      - "(?i)nuclear"
    series_description_patterns:
      - "(?i)pet"
      - "(?i)suv"
      - "(?i)emission"
      - "(?i)attenuation.*corrected"
      - "(?i)ac.*pet"
      - "(?i)nac.*pet"
    modality: PT
    priority: 15
    enabled: true

  - body_parts:
      - WHOLEBODY
      - CHEST
      - ABDOMEN
    study_description_patterns:
      - "(?i)pet.*ct"
      - "(?i)fdg"
    series_description_patterns:
      - "(?i)ct.*ac"
      - "(?i)low.*dose.*ct"
      - "(?i)attenuation"
    modality: CT
    priority: 14
    enabled: true
```

---

### File: `backend/app/usecases/pet_ct/model/inference_config.yaml`

```yaml
model:
  architecture: swin_unetr
  # Primary: MONAI Model Zoo auto-download
  # Fallback: custom trained weights
  bundle_name: wholeBody_ct_segmentation   # for anatomical CT segmentation
  source: monai_model_zoo
  version: "1.0.0"
  bundle_cache_dir: /app/app/usecases/pet_ct/model/bundles
  # Optional: path to custom PET lesion detection weights
  # Set to null to use SUV-threshold-based approach (no deep learning required)
  custom_pet_weights_path: null

inference:
  device: auto   # "cuda", "cpu", "auto"
  mixed_precision: true
  batch_size: 1
  sliding_window:
    roi_size: [96, 96, 96]
    sw_batch_size: 1
    overlap: 0.5
    mode: gaussian

preprocessing:
  # PET preprocessing
  pet:
    suv_normalisation: bodyweight   # "bodyweight" | "lean_body_mass" | "bsa"
    # SUV = (activity_Bq_per_mL * body_weight_kg * 1000) / injected_dose_Bq
    target_spacing: [2.0, 2.0, 2.0]   # resample PET to this spacing (mm)
    orientation: RAS
    clip_suv_max: 30.0                # clip extreme SUV values before model
    suv_threshold_for_lesion: 2.5    # used if no DL model (threshold segmentation)
    liver_reference_sphere_radius_mm: 15.0  # for PERCIST reference ROI

  # CT preprocessing
  ct:
    target_spacing: [2.0, 2.0, 2.0]
    orientation: RAS
    window_center: 40    # soft tissue window
    window_width: 400
    clip_hu_min: -1000
    clip_hu_max: 1000
    register_to_pet: true   # rigid registration if CT/PET spacing differs

postprocessing:
  label_map:
    0: background
    1: fdg_avid_lesion
  # Minimum lesion criteria
  min_lesion_volume_ml: 0.5          # filter sub-threshold detections
  min_suv_mean: 2.5                  # minimum SUVmean to keep a lesion
  apply_connected_components: true

  # PERCIST 1.0 thresholding
  percist:
    enabled: true
    # Reference: SUV_lean ≥ 1.5 × (liver_mean + 2 × liver_SD) AND MTV ≥ 1 cm³
    reference_organ: liver
    response_criteria:
      complete_response:   suv_decrease_pct: 100   # no residual uptake
      partial_response:    suv_decrease_pct: 30    # ≥30% decrease SUVpeak
      stable_disease:      suv_change_pct:   30    # < 30% change
      progressive_disease: suv_increase_pct: 30   # ≥30% increase OR new lesion

  # Deauville 5-point scale (for lymphoma)
  deauville:
    enabled: true
    reference_organs:
      background_blood_pool: mediastinum
      liver: liver

quality_checks:
  # PET-specific QA
  min_pet_slices: 50
  max_scan_to_injection_minutes: 90  # typical 60 min uptake; flag >90 min
  min_injected_dose_MBq: 100
  max_injected_dose_MBq: 600
  required_dicom_tags:
    - RadiopharmaceuticalStartTime
    - RadionuclideTotalDose
    - PatientWeight
  # CT QA
  min_ct_slices: 50
  ct_must_be_diagnostic_quality: false   # low-dose AC-CT is acceptable
```

---

### File: `backend/app/usecases/pet_ct/pipeline.py`

Implement `class Pipeline(BasePipeline)` with the following specification.
Follow this **exactly** — field names in the return dict must match
`outputs_schema.json`.

```python
"""
PET/CT whole-body oncology pipeline.

Phases:
  preprocess  → download DICOM, extract SUV normalisation params, convert to
                NIfTI, co-register CT to PET, run QA checks
  infer       → segmentation of FDG-avid lesions (DL model OR SUV-threshold),
                anatomical CT organ segmentation for reference regions
  postprocess → per-lesion SUV metrics, MTV, TLG, PERCIST, Deauville,
                overlay generation, report JSON
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import nibabel as nib
import pydicom
import SimpleITK as sitk
import yaml

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.usecases.base import BasePipeline

CONFIG_PATH = Path(__file__).parent / "model" / "inference_config.yaml"


class Pipeline(BasePipeline):

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._config = yaml.safe_load(f)
        self._model = None
        self._device = None

    # ── PREPROCESS ────────────────────────────────────────────────────────────
    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        """
        Steps:
        1.  Classify series into PET (modality=PT) and CT (modality=CT)
        2.  Download both series from Orthanc via pacs client
        3.  Extract SUV normalisation parameters from PET DICOM headers:
              - RadiopharmaceuticalStartTime
              - RadionuclideTotalDose  (Bq)
              - PatientWeight          (kg)
              - AcquisitionTime        (for decay correction)
              - Radiopharmaceutical    (to detect FDG vs amyloid vs PSMA)
        4.  Convert PET DICOM → NIfTI (units: Bq/mL)
        5.  Apply SUV normalisation → SUV NIfTI (dimensionless)
              SUV = (pixel_value_Bq_mL * patient_weight_g) / injected_dose_Bq
        6.  Convert CT DICOM → NIfTI (units: HU)
        7.  Rigid-register CT to PET if voxel grids differ (use SimpleITK)
        8.  Run QA checks (see _run_qa below)
        9.  Save:
              working_dir/pet_suv.nii.gz
              working_dir/ct_hu.nii.gz
              working_dir/suv_params.json

        Return dict keys:
          pet_suv_path:           str
          ct_hu_path:             str
          suv_params:             dict   # dose, weight, half_life, scan_time
          radiopharmaceutical:    str    # "FDG" | "PSMA" | "DOTATATE" | ...
          pet_series_uid:         str
          ct_series_uid:          str
          qa_flags:               list[str]
          qa_details:             dict
          study_uid:              str
        """
        ...  # implement

    # ── INFER ─────────────────────────────────────────────────────────────────
    def infer(
        self, preprocessed: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        """
        Two sub-tasks run sequentially:

        Task A — PET Lesion Segmentation
          If custom_pet_weights_path is set:
            Load SwinUNETR model, run sliding-window inference on SUV volume.
            Input:  [1-channel SUV, 1-channel CT_HU] concatenated → 2-channel
            Output: binary lesion mask (sigmoid > 0.5)
          Else (no DL weights):
            SUV-threshold segmentation:
              liver_mean, liver_sd = measure SUV in liver reference sphere
              threshold = max(suv_threshold_for_lesion,
                              liver_mean + 2 * liver_sd)
              lesion_mask = suv_volume > threshold
              apply connected components → remove < min_lesion_volume_ml

        Task B — CT Organ Segmentation (for reference regions)
          Use MONAI wholeBody_ct_segmentation bundle (auto-download).
          Extract label masks for: liver, mediastinum, aorta, spleen
          These provide reference SUV values for PERCIST and Deauville.

        Save:
          working_dir/lesion_mask.nii.gz
          working_dir/organ_mask.nii.gz
          working_dir/inference_meta.json   # threshold used, device, model_ver

        Return dict keys (merge preprocessed + new keys):
          lesion_mask_path:     str
          organ_mask_path:      str
          inference_meta:       dict
          **preprocessed            (all keys from preprocess phase)
        """
        ...  # implement

    # ── POSTPROCESS ───────────────────────────────────────────────────────────
    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        """
        Compute all clinical metrics and generate artifacts.

        METRIC COMPUTATION STEPS:

        1. Load SUV volume and lesion mask
        2. Extract individual lesions via connected-component labelling
        3. Per lesion compute:
             SUVmax    = max voxel SUV within lesion
             SUVmean   = mean voxel SUV within lesion
             SUVpeak   = mean SUV within 1 cm³ sphere centred on SUVmax voxel
             volume_ml = voxel_count × voxel_volume_ml
             TLG       = SUVmean × volume_ml
             centroid_voxel    = [x, y, z]
             anatomical_region = nearest organ from organ_mask
        4. Whole-body aggregates:
             MTV_total   = sum of all lesion volumes (mL)
             TLG_total   = sum of all lesion TLGs
             SUVmax_body = highest SUVmax across all lesions
             lesion_count = number of discrete lesions
        5. Reference organ metrics (from organ_mask + SUV):
             liver_suv_mean, liver_suv_sd     (PERCIST reference)
             mediastinum_suv_mean             (Deauville reference)
        6. PERCIST score (if enabled):
             Compare SUVpeak_max to liver reference:
             PERCIST_threshold = 1.5 × (liver_mean + 2 × liver_sd)
             classify: CMR | PMR | SMD | PMD
        7. Deauville score (if enabled, for lymphoma):
             Score 1–5 based on lesion SUV vs mediastinum and liver
        8. Generate artifacts:
             pet_suv.nii.gz           (original SUV volume)
             lesion_mask.nii.gz       (binary lesion segmentation)
             organ_mask.nii.gz        (anatomical organ labels)
             pet_ct_overlay_axial.png (MIP or fused axial montage)
             pet_mip_coronal.png      (maximum intensity projection, coronal)
             report.json              (full metrics dump)

        RETURN DICT — must match outputs_schema.json exactly:

        {
          "summary": {
            "lesions_detected":      bool,
            "lesion_count":          int,
            "mtv_total_ml":          float,
            "tlg_total":             float,
            "suvmax_body":           float,
            "radiopharmaceutical":   str,
            "percist_score":         str | null,   # "CMR"|"PMR"|"SMD"|"PMD"|null
            "deauville_score":       int | null,   # 1-5 | null
            "processing_notes":      str,
          },
          "measurements": {
            "lesions": [
              {
                "id":                int,
                "suv_max":           float,
                "suv_mean":          float,
                "suv_peak":          float,
                "volume_ml":         float,
                "tlg":               float,
                "anatomical_region": str,
                "centroid_voxel":    [int, int, int],
              },
              ...
            ],
            "reference_organs": {
              "liver_suv_mean":       float,
              "liver_suv_sd":         float,
              "mediastinum_suv_mean": float,
            },
            "whole_body": {
              "mtv_total_ml":  float,
              "tlg_total":     float,
              "suvmax_body":   float,
              "lesion_count":  int,
            },
            "voxel_spacing_mm":  [float, float, float],
            "image_dimensions":  [int, int, int],
          },
          "qa_flags":      list[str],
          "qa_details":    dict,
          "model_version": str,
          "model_checksum": str,
          "artifacts": [
            {
              "name":          "pet_suv",
              "artifact_type": "pet_nifti",
              "local_path":    str,
              "content_type":  "application/gzip",
            },
            {
              "name":          "lesion_mask",
              "artifact_type": "segmentation_nifti",
              "local_path":    str,
              "content_type":  "application/gzip",
            },
            {
              "name":          "organ_mask",
              "artifact_type": "organ_nifti",
              "local_path":    str,
              "content_type":  "application/gzip",
            },
            {
              "name":          "pet_ct_overlay_axial",
              "artifact_type": "overlay_png",
              "local_path":    str,
              "content_type":  "image/png",
            },
            {
              "name":          "pet_mip_coronal",
              "artifact_type": "mip_png",
              "local_path":    str,
              "content_type":  "image/png",
            },
            {
              "name":          "report",
              "artifact_type": "report_json",
              "local_path":    str,
              "content_type":  "application/json",
            },
          ],
        }
        """
        ...  # implement

    # ── PRIVATE HELPERS ───────────────────────────────────────────────────────

    def _classify_pet_ct_series(
        self, series: list[Series]
    ) -> tuple[list[Series], list[Series]]:
        """Return (pet_series, ct_series) separated by DICOM Modality tag."""
        ...

    def _extract_suv_params(self, dicom_dir: str) -> dict[str, Any]:
        """
        Read DICOM headers from PET series to extract:
          injected_dose_Bq     float   (RadionuclideTotalDose × decay_correction)
          patient_weight_kg    float
          scan_start_time      datetime
          injection_time       datetime
          half_life_seconds    float   (Radiopharmaceutical half-life)
          radiopharmaceutical  str     ("FDG" | "PSMA-617" | "DOTATATE" | ...)
        Apply decay correction:
          dose_at_scan = injected_dose × exp(-ln(2) × elapsed_seconds / half_life)
        """
        ...

    def _dicom_to_suv_nifti(
        self, dicom_dir: str, suv_params: dict, output_path: str
    ) -> str:
        """
        Convert PET DICOM (pixel values in Bq/mL after RescaleSlope/Intercept)
        to SUV NIfTI using extracted suv_params.
        SUV = (Bq_per_mL × weight_g) / dose_at_scan_Bq
        Write to output_path. Return output_path.
        """
        ...

    def _dicom_to_hu_nifti(self, dicom_dir: str, output_path: str) -> str:
        """Convert CT DICOM to HU NIfTI. Apply RescaleSlope/Intercept."""
        ...

    def _register_ct_to_pet(
        self, pet_path: str, ct_path: str, output_path: str
    ) -> str:
        """
        Rigid registration using SimpleITK.
        Register CT to PET space (PET is fixed, CT is moving).
        Use mutual information as metric. Write resampled CT to output_path.
        """
        ...

    def _run_qa(
        self, suv_params: dict, pet_path: str, ct_path: str
    ) -> tuple[list[str], dict]:
        """
        Run PET/CT-specific quality checks. Return (qa_flags, qa_details).

        Checks:
        - missing_suv_params:      any required DICOM tag absent
        - scan_delay_exceeded:     scan_start - injection_time > 90 min
        - dose_out_of_range:  
             injected_dose < 100 MBq or > 600 MBq
        - weight_missing:          PatientWeight tag absent
        - insufficient_pet_slices: < min_pet_slices
        - insufficient_ct_slices:  < min_ct_slices
        - suv_range_suspicious:    max SUV > clip_suv_max (possible calibration error)
        """
        ...

    def _compute_suv_peak(
        self, suv_vol: np.ndarray, mask: np.ndarray,
        voxel_spacing_mm: list[float], sphere_radius_mm: float = 6.2
    ) -> float:
        """
        SUVpeak = mean SUV within a 1 cm³ sphere (~6.2 mm radius) centred on
        the SUVmax voxel within the mask. Standard PERCIST definition.
        """
        ...

    def _deauville_score(
        self, suvmax_lesion: float,
        mediastinum_mean: float, liver_mean: float
    ) -> int:
        """
        Deauville 5-point scale:
          1 = no uptake
          2 = uptake ≤ mediastinum
          3 = mediastinum < uptake ≤ liver
          4 = uptake moderately > liver
          5 = uptake markedly > liver OR new lesion
        Return int 1–5.
        """
        ...

    def _generate_mip_png(
        self, suv_vol: np.ndarray, lesion_mask: np.ndarray,
        output_path: str, plane: str = "coronal"
    ) -> str:
        """
        Generate maximum intensity projection PNG.
        Overlay lesion mask in red on grey-scale MIP.
        plane: "coronal" | "sagittal" | "axial"
        """
        ...

    def _get_model_checksum(self) -> str:
        """SHA-256 of model weights file, first 16 hex chars."""
        ...
```

---

### File: `backend/app/usecases/pet_ct/outputs_schema.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PET/CT Oncology Analysis Result",
  "type": "object",
  "required": ["summary", "measurements", "qa_flags", "model_version", "artifacts"],
  "properties": {
    "summary": {
      "type": "object",
      "required": ["lesions_detected", "lesion_count", "mtv_total_ml", "tlg_total", "suvmax_body"],
      "properties": {
        "lesions_detected":    { "type": "boolean" },
        "lesion_count":        { "type": "integer", "minimum": 0 },
        "mtv_total_ml":        { "type": "number",  "minimum": 0 },
        "tlg_total":           { "type": "number",  "minimum": 0 },
        "suvmax_body":         { "type": "number",  "minimum": 0 },
        "radiopharmaceutical": { "type": "string" },
        "percist_score":       { "type": ["string", "null"], "enum": ["CMR","PMR","SMD","PMD",null] },
        "deauville_score":     { "type": ["integer", "null"], "minimum": 1, "maximum": 5 },
        "processing_notes":    { "type": "string" }
      }
    },
    "measurements": {
      "type": "object",
      "properties": {
        "lesions": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["id", "suv_max", "suv_mean", "suv_peak", "volume_ml", "tlg"],
            "properties": {
              "id":                { "type": "integer" },
              "suv_max":           { "type": "number" },
              "suv_mean":          { "type": "number" },
              "suv_peak":          { "type": "number" },
              "volume_ml":         { "type": "number" },
              "tlg":               { "type": "number" },
              "anatomical_region": { "type": "string" },
              "centroid_voxel":    { "type": "array", "items": { "type": "integer" }, "minItems": 3, "maxItems": 3 }
            }
          }
        },
        "reference_organs": {
          "type": "object",
          "properties": {
            "liver_suv_mean":        { "type": "number" },
            "liver_suv_sd":          { "type": "number" },
            "mediastinum_suv_mean":  { "type": "number" }
          }
        },
        "whole_body": {
          "type": "object",
          "properties": {
            "mtv_total_ml":  { "type": "number" },
            "tlg_total":     { "type": "number" },
            "suvmax_body":   { "type": "number" },
            "lesion_count":  { "type": "integer" }
          }
        },
        "voxel_spacing_mm": { "type": "array", "items": { "type": "number" }, "minItems": 3, "maxItems": 3 },
        "image_dimensions": { "type": "array", "items": { "type": "integer" }, "minItems": 3, "maxItems": 3 }
      }
    },
    "qa_flags":       { "type": "array",  "items": { "type": "string" } },
    "qa_details":     { "type": "object", "additionalProperties": true },
    "model_version":  { "type": "string" },
    "model_checksum": { "type": "string" },
    "artifacts": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "artifact_type", "local_path"],
        "properties": {
          "name":          { "type": "string" },
          "artifact_type": { "type": "string" },
          "local_path":    { "type": "string" },
          "content_type":  { "type": "string" }
        }
      }
    }
  }
}
```

---

### File: `backend/app/usecases/pet_ct/ui_schema.json`

```json
{
  "title": "PET/CT Oncology Analysis",
  "description": "Whole-body FDG-PET/CT lesion detection, SUV quantification, MTV, TLG, PERCIST & Deauville scoring",
  "sections": [
    {
      "id": "summary",
      "title": "Clinical Summary",
      "type": "key_value",
      "data_path": "summary",
      "fields": [
        { "key": "lesions_detected",    "label": "FDG-Avid Lesions Found", "format": "boolean" },
        { "key": "lesion_count",        "label": "Total Lesion Count",      "format": "number" },
        { "key": "mtv_total_ml",        "label": "Metabolic Tumour Volume", "unit": "mL",  "format": "number", "precision": 1 },
        { "key": "tlg_total",           "label": "Total Lesion Glycolysis", "unit": "g",   "format": "number", "precision": 1 },
        { "key": "suvmax_body",         "label": "Whole-Body SUVmax",       "format": "number", "precision": 2 },
        { "key": "radiopharmaceutical", "label": "Radiopharmaceutical",     "format": "text" },
        { "key": "percist_score",       "label": "PERCIST 1.0 Score",       "format": "text" },
        { "key": "deauville_score",     "label": "Deauville Score (1–5)",   "format": "number" },
        { "key": "processing_notes",    "label": "Notes",                   "format": "text" }
      ]
    },
    {
      "id": "lesion_table",
      "title": "Lesion-Level Metrics",
      "type": "table",
      "data_path": "measurements.lesions",
      "columns": [
        { "key": "id",                "label": "#",              "format": "number" },
        { "key": "anatomical_region", "label": "Location",       "format": "text" },
        { "key": "suv_max",           "label": "SUVmax",         "format": "number", "precision": 2 },
        { "key": "suv_mean",          "label": "SUVmean",        "format": "number", "precision": 2 },
        { "key": "suv_peak",          "label": "SUVpeak",        "format": "number", "precision": 2 },
        { "key": "volume_ml",         "label": "Volume (mL)",    "format": "number", "precision": 1 },
        { "key": "tlg",               "label": "TLG (g)",        "format": "number", "precision": 1 }
      ]
    },
    {
      "id": "reference_organs",
      "title": "Reference Organ SUV",
      "type": "key_value",
      "data_path": "measurements.reference_organs",
      "fields": [
        { "key": "liver_suv_mean",       "label": "Liver SUVmean",        "format": "number", "precision": 2 },
        { "key": "liver_suv_sd",         "label": "Liver SUV SD",         "format": "number", "precision": 2 },
        { "key": "mediastinum_suv_mean", "label": "Mediastinum SUVmean",  "format": "number", "precision": 2 }
      ]
    },
    {
      "id": "qa",
      "title": "Quality Assurance",
      "type": "qa_panel",
      "flags_path": "qa_flags",
      "details_path": "qa_details",
      "severity_map": {
        "missing_suv_params":    "error",
        "scan_delay_exceeded":   "warning",
        "dose_out_of_range":     "warning",
        "weight_missing":        "error",
        "insufficient_pet_slices": "error",
        "insufficient_ct_slices":  "warning",
        "suv_range_suspicious":  "warning"
      }
    },
    {
      "id": "overlays",
      "title": "PET/CT Overlay",
      "type": "overlay",
      "artifact_filter": "overlay_png",
      "colormap": {
        "1": { "label": "FDG-Avid Lesion", "color": "#FF4444" }
      }
    },
    {
      "id": "mip",
      "title": "Maximum Intensity Projection",
      "type": "image",
      "artifact_filter": "mip_png"
    },
    {
      "id": "model_info",
      "title": "Model Information",
      "type": "key_value",
      "fields": [
        { "key": "model_version",  "label": "Model Version",  "format": "text", "data_path": "model_version" },
        { "key": "model_checksum", "label": "Model Checksum", "format": "text", "data_path": "model_checksum" }
      ]
    }
  ]
}
```

---

---

## PLUGIN 2 — `pet_ct_brain` (Brain / Neuro PET)

### Clinical purpose
Brain PET/CT for neurology and neuro-oncology: FDG metabolic mapping,
amyloid plaque detection (Florbetapir/Flutemetamol), tau imaging, and
PSMA for brain metastases. Produces regional cortical SUV ratios (SUVR),
asymmetry indices, and reference-to-cerebellum standardisation.

---

### File: `backend/app/usecases/pet_ct_brain/manifest.yaml`

```yaml
name: pet_ct_brain
version: "1.0.0"
description: >
  Brain PET/CT pipeline for neurology and neuro-oncology. Supports FDG
  (metabolic mapping), amyloid (Florbetapir, Flutemetamol), tau, and PSMA
  radiotracers. Computes regional cortical SUVR, asymmetry index, and
  compares to age-matched normative atlases. Detects hypometabolism patterns
  consistent with Alzheimer's, FTD, and DLB, and flags focal hypermetabolism
  for tumour recurrence evaluation.
supported_body_parts:
  - BRAIN
  - HEAD
required_sequences:
  - PET
  - CT
model_type: atlas_registration
enabled: true
```

---

### File: `backend/app/usecases/pet_ct_brain/routing_rules.yaml`

```yaml
rules:
  - body_parts:
      - BRAIN
      - HEAD
    study_description_patterns:
      - "(?i)brain.*pet"
      - "(?i)pet.*brain"
      - "(?i)fdg.*brain"
      - "(?i)amyloid"
      - "(?i)tau.*pet"
      - "(?i)psma.*brain"
      - "(?i)neuro.*pet"
      - "(?i)dementia.*pet"
      - "(?i)alzheimer"
    series_description_patterns:
      - "(?i)pet"
      - "(?i)suv"
      - "(?i)emission"
      - "(?i)ac.*pet"
    modality: PT
    priority: 20
    enabled: true
```

---

### File: `backend/app/usecases/pet_ct_brain/model/inference_config.yaml`

```yaml
model:
  architecture: atlas_registration   # MNI152 atlas-based region extraction
  source: mni152_aal3                # AAL3 atlas for brain parcellation
  version: "1.0.0"
  bundle_cache_dir: /app/app/usecases/pet_ct_brain/model/bundles
  # Optional: trained DL model for amyloid positivity classification
  amyloid_classifier_weights: null
  # Optional: trained DL model for FDG pattern classification (AD/FTD/DLB/Normal)
  fdg_pattern_classifier_weights: null

inference:
  device: auto
  registration:
    method: rigid_then_affine   # register PET to MNI152 via CT
    metric: mutual_information
    optimizer: gradient_descent
    iterations: [100, 50, 25]   # multi-resolution
  atlas:
    name: AAL3                  # Automated Anatomical Labelling 3
    regions: 170                # bilateral cortical + subcortical regions
    # Reference region for SUVR normalisation per tracer:
    reference_regions:
      FDG:         pons          # brainstem pons (metabolically stable)
      amyloid:     cerebellum    # whole cerebellum
      tau:         cerebellum
      PSMA:        cerebellum

preprocessing:
  pet:
    suv_normalisation: bodyweight
    target_spacing: [1.0, 1.0, 1.0]
    orientation: RAS
    skull_strip: true            # remove skull from PET before atlas registration
    smooth_fwhm_mm: 8.0          # Gaussian smoothing to match scanner resolution
  ct:
    target_spacing: [1.0, 1.0, 1.0]
    orientation: RAS
    brain_extraction: true       # extract brain mask from CT

postprocessing:
  # SUVR = mean_SUV_target_region / mean_SUV_reference_region
  compute_suvr: true
  asymmetry_index: true          # AI = (L-R)/(L+R) × 100 per region pair
  # Global amyloid burden (centiloid scale) for amyloid tracers
  centiloid: true
  # Z-score vs normative database (if available)
  normative_comparison: false    # set true when normative DB is loaded
  min_region_volume_ml: 0.5     # skip regions smaller than this

quality_checks:
  min_pet_slices: 80
  max_scan_to_injection_minutes: 75   # brain PET: tighter window
  skull_strip_failure_threshold: 0.3  # flag if <30% brain voxels found
  required_dicom_tags:
    - RadiopharmaceuticalStartTime
    - RadionuclideTotalDose
    - PatientWeight
    - Radiopharmaceutical
```

---

### File: `backend/app/usecases/pet_ct_brain/pipeline.py`

```python
"""
Brain PET/CT pipeline.

Phases:
  preprocess  → download DICOM, extract tracer info, skull-strip, convert to
                NIfTI, co-register PET→MNI152 via CT, apply AAL3 parcellation
  infer       → extract regional SUV per AAL3 ROI, compute SUVR per region,
                optional DL-based amyloid/FDG pattern classification
  postprocess → regional SUVR table, asymmetry index, global centiloid,
                pattern classification label, overlay PNGs, report JSON
"""
from __future__ import annotations
from app.usecases.base import BasePipeline
from typing import Any

class Pipeline(BasePipeline):

    def preprocess(self, study, series, working_dir, pacs, event_loop=None) -> dict:
        """
        Steps:
        1. Separate PET (modality=PT) and CT (modality=CT) series
        2. Detect radiopharmaceutical from DICOM Radiopharmaceutical tag:
             → "FDG" | "Florbetapir" | "Flutemetamol" | "Flortaucipir" | "PSMA" | "unknown"
        3. Extract SUV normalisation parameters (same as pet_ct pipeline)
        4. Convert PET DICOM → SUV NIfTI
        5. Convert CT DICOM → HU NIfTI
        6. Brain extraction (skull strip) on CT using SimpleITK:
             Otsu threshold on HU → dilate → fill → mask
        7. Register CT brain to MNI152 template (affine):
             Use MONAI registration or ANTsPy (register CT→MNI152)
        8. Apply same transform to PET → PET now in MNI152 space
        9. Apply AAL3 atlas → each voxel labelled with ROI index
        10. Smooth PET in MNI space (FWHM = smooth_fwhm_mm)
        11. Run QA

        Return keys:
          pet_suv_mni_path:       str   (PET in MNI space)
          ct_brain_mni_path:      str   (CT brain in MNI space)
          aal3_path:              str   (AAL3 label volume in MNI space)
          suv_params:             dict
          radiopharmaceutical:    str
          reference_region:       str   (from config per tracer)
          qa_flags:               list[str]
          qa_details:             dict
          study_uid:              str
        """
        ...

    def infer(self, preprocessed, working_dir) -> dict:
        """
        1. Load PET SUV volume (MNI space) and AAL3 atlas
        2. For each AAL3 region (170 regions):
             mask = aal3_volume == region_id
             region_suv_mean = mean(suv_vol[mask])
             region_suv_max  = max(suv_vol[mask])
             region_volume_ml = voxel_count × voxel_vol_ml
        3. Identify reference region voxels:
             ref_mask = aal3_volume == reference_region_id
             reference_suv_mean = mean(suv_vol[ref_mask])
        4. SUVR per region = region_suv_mean / reference_suv_mean
        5. Asymmetry index per bilateral pair:
             AI = (left_suvr - right_suvr) / (left_suvr + right_suvr) × 100
        6. Global SUVR = mean SUVR across all cortical regions
        7. If amyloid tracer AND classifier weights set:
             Classify amyloid positive/negative + centiloid score
        8. If FDG AND classifier weights set:
             Classify pattern: Normal | AD-like | FTD-like | DLB-like | Tumour-recurrence

        Return dict:
          regional_suvr:         dict[region_name → suvr_float]
          regional_suv_mean:     dict[region_name → suv_mean_float]
          regional_suv_max:      dict[region_name → suv_max_float]
          regional_volume_ml:    dict[region_name → volume_float]
          asymmetry_index:       dict[region_pair → ai_float]
          global_suvr:           float
          reference_suv_mean:    float
          amyloid_positive:      bool | null
          centiloid:             float | null
          fdg_pattern:           str | null
          **preprocessed
        """
        ...

    def postprocess(self, inference_output, working_dir) -> dict:
        """
        RETURN DICT — must match outputs_schema.json:

        {
          "summary": {
            "tracer":             str,
            "global_suvr":        float,
            "reference_region":   str,
            "amyloid_positive":   bool | null,
            "centiloid":          float | null,
            "fdg_pattern":        str | null,
            "most_hypometabolic_region": str | null,
            "most_hypermetabolic_region": str | null,
            "processing_notes":   str,
          },
          "measurements": {
            "regional": [
              {
                "region":       str,
                "suvr":         float,
                "suv_mean":     float,
                "suv_max":      float,
                "volume_ml":    float,
                "ai":           float | null,   # asymmetry index
              },
              ...
            ],
            "reference": {
              "region":        str,
              "suv_mean":      float,
            },
            "voxel_spacing_mm":  [float, float, float],
            "image_dimensions":  [int, int, int],
          },
          "qa_flags":      list[str],
          "qa_details":    dict,
          "model_version": str,
          "model_checksum": str,
          "artifacts": [
            { "name": "pet_suv_mni",       "artifact_type": "pet_nifti",      ... },
            { "name": "aal3_parcellation", "artifact_type": "atlas_nifti",    ... },
            { "name": "suvr_surface_axial","artifact_type": "overlay_png",    ... },
            { "name": "report",            "artifact_type": "report_json",    ... },
          ],
        }
        """
        ...
```

---

---

## SHARED REQUIREMENTS

### Python dependencies to add to `backend/requirements.txt`

```
# PET/CT — already present (check before adding):
# SimpleITK, nibabel, pydicom, numpy, scipy, torch, monai

# New additions needed:
antspyx>=0.4.2              # ANTs registration (PET→MNI152)
scikit-image>=0.21          # connected components, sphere erosion
matplotlib>=3.8             # MIP PNG generation
nilearn>=0.10               # brain atlas handling, surface plots (optional)
```

---

### DICOM Tag Reference for PET SUV Calculation

```
(0054,0016) → RadiopharmaceuticalInformationSequence
  (0018,1072) → RadiopharmaceuticalStartTime   "HHMMSS.fraction"
  (0018,1074) → RadionuclideTotalDose          float  (Bq)
  (0018,1075) → RadionuclideHalfLife           float  (seconds)
  (0054,0300) → RadionuclideCodeSequence
    (0008,0104) → CodeMeaning                  str    e.g. "^18^Fluorine"
  (0018,0031) → Radiopharmaceutical            str    e.g. "FDG"

(0010,1030) → PatientWeight                    float  (kg)
(0008,0032) → AcquisitionTime                  str    "HHMMSS"

Pixel value → Bq/mL:
  bq_ml = pixel_value × RescaleSlope + RescaleIntercept
  (where tags 0028,1053 and 0028,1052 are used)
```

---

### QA Flag Definitions (both pipelines)

| Flag | Severity | Condition |
|------|----------|-----------|
| `missing_suv_params` | BLOCKING | Any SUV DICOM tag absent |
| `scan_delay_exceeded` | WARNING | Uptake time > 90 min (whole-body) or 75 min (brain) |
| `dose_out_of_range` | WARNING | Injected dose < 100 MBq or > 600 MBq |
| `weight_missing` | BLOCKING | PatientWeight tag absent (cannot compute SUV) |
| `insufficient_pet_slices` | BLOCKING | Fewer slices than min_pet_slices |
| `insufficient_ct_slices` | WARNING | Fewer slices than min_ct_slices |
| `suv_range_suspicious` | WARNING | Max SUV > clip_suv_max (scanner calibration issue) |
| `skull_strip_failure` | WARNING | Brain extraction found < 30% brain voxels |
| `registration_failed` | BLOCKING | CT-to-MNI registration error > 10 mm |

---

### Testing

After implementation, verify with:

```bash
# Unit test SUV calculation
python -m pytest backend/tests/usecases/pet_ct/ -v

# Integration test: submit a PET/CT study via API
curl -X POST http://localhost:8000/api/studies/{uid}/jobs \
  -H "Content-Type: application/json" \
  -d '{"usecase_names": ["pet_ct"]}'

# Check job status
curl http://localhost:8000/api/jobs/{job_id}

# Retrieve result
curl http://localhost:8000/api/results/{uid}/pet_ct
```

---

### Notes for the implementer

1. **Class name must be exactly `Pipeline`** — the registry does `getattr(module, "Pipeline")`.
2. **postprocess return dict keys must exactly match `outputs_schema.json`** — the orchestrator validates against it.
3. **All file paths in `artifacts[*].local_path` must be absolute paths within `working_dir`**.
4. **SUV calculation is the most failure-prone step** — validate DICOM tags before attempting; set clear QA flags when missing.
5. **PET/CT registration**: if CT and PET share the same DICOM frame of reference (FrameOfReferenceUID matches), skip registration — they are already co-registered by the scanner.
6. **`event_loop` parameter in preprocess**: use it when calling `asyncio.run_coroutine_threadsafe(pacs.download(...), event_loop)` since preprocess runs in a Celery worker (sync context).
7. **MNI152 template**: download from `nilearn.datasets.load_mni152_template()` on first run; cache locally.
8. **AAL3 atlas**: available via `nilearn.datasets.fetch_atlas_aal(version='SPM12')` — 170 ROIs, standard in clinical neuro PET reporting.
