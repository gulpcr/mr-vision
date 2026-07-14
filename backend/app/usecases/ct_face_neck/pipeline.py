"""CT Face & Neck pipeline — Gemini VLM structured extraction.

No local segmentation/classification network: Gemini itself is the "model". The
pipeline downloads the CT series, renders windowed axial cross-sections, and asks
Gemini to extract a structured per-region finding set (parotid, cervical lymph
nodes, mandible/maxilla, airway, oropharynx, larynx, thyroid, nasal septum)
following the reporting convention of a de-identified reference case baked into
the prompt as a few-shot example.

Phases:
  preprocess  — select the CT series, download DICOMs via PACSClient, extract
                clinical-history text + demographics from the DICOM header, and
                render soft-tissue/bone-window preview cross-sections
  infer       — call Gemini with the preview images + prompt, constrained to
                response_schema, with retry; falls back to a low-confidence
                placeholder extraction if Gemini is disabled/unavailable
  postprocess — assemble the outputs_schema.json-conformant result dict and
                save preview images + the raw extraction as artifacts

Important limitation: BasePipeline.preprocess/infer only ever see the CURRENT
study — there is no injection point for a prior study's images. True cross-visit
numeric trending is therefore NOT computed here; it already happens generically
for every use case via infrastructure/queue/tasks.py's LongitudinalAnalysisService,
which compares this pipeline's `measurements` against prior stored `measurements`
for the same patient (Settings.longitudinal_enabled) and writes the result to
summary.longitudinal_analysis. This pipeline's own measurements.longitudinal_comparison
only reflects what the CURRENT study's own clinical-history text says about a
prior comparison (e.g. "comparative study with previous CT dated ...") — it is a
text-derived hint, not an image-based measurement delta.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
import SimpleITK as sitk
import structlog
import yaml
from PIL import Image

from app.config import get_settings
from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.infrastructure.llm.gemini_client import GeminiClient
from app.usecases.base import BasePipeline

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

_REGION_KEYS = (
    "right_parotid_region",
    "lymphnodes_right_neck",
    "lymphnodes_left_neck",
    "mandible_and_maxilla",
    "supra_glottic_region",
    "oropharynx",
    "laryngeal_structures",
    "thyroid_gland",
    "nasal_septum",
)

# DICOM tags that may carry clinical-history/indication free text. tasks.py's
# Study construction only sets patient_id/name/study_description/modality (see
# infrastructure/queue/tasks.py:337), so age/sex/clinical-indication text are not
# otherwise available to the pipeline and must be read from the DICOM header directly.
_CLINICAL_TEXT_TAGS = (
    "ReasonForStudy",
    "AdmittingDiagnosesDescription",
    "StudyComments",
    "RequestedProcedureDescription",
)

_COMPARISON_MENTION_RE = re.compile(r"(?i)comparat\w*|previous\s+(?:ct|study|scan)")
_COMPARISON_DATE_RE = re.compile(
    r"(?i)previous[^.]{0,40}?(?:dated?|study)?\s*(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})"
)


# ── Few-shot exemplar (de-identified reference case) ───────────────────────────
# Calibrates vocabulary/granularity only. No real patient data is embedded here —
# name, contact, PRN, and accession fields from the source report are deliberately
# excluded to avoid sending PHI to a third-party LLM API.

_FEW_SHOT_CLINICAL_CONTEXT = (
    "63-year-old female. Known case of carcinoma of the right parotid gland, status "
    "post-surgery and radiation. Comparative study requested against a prior CT."
)

_FEW_SHOT_OUTPUT: dict[str, Any] = {
    "findings": {
        "right_parotid_region": {
            "finding": "No evidence of residual or recurrent mass in the right parotid region.",
            "status": "no_residual_recurrent_mass",
            "post_treatment_state": "post_surgery_and_radiation",
            "comparison_to_baseline": "regressed",
        },
        "lymphnodes_right_neck": {
            "finding": (
                "Small sub-centimeter lymph nodes in the right neck, reduced in size "
                "and number compared to the prior study."
            ),
            "status": "subcentimeter_nodes",
            "count": None,
            "largest_short_axis_mm": None,
            "comparison_to_baseline": "regression_in_size_and_number",
        },
        "lymphnodes_left_neck": {
            "finding": "Small sub-centimeter lymph nodes also noted in the left neck.",
            "status": "subcentimeter_nodes",
            "count": None,
            "largest_short_axis_mm": None,
            "comparison_to_baseline": "stable",
        },
        "mandible_and_maxilla": {
            "finding": "No destruction or erosion of the mandible or maxilla.",
            "status": "normal",
        },
        "supra_glottic_region": {
            "finding": "No mass in the supra-glottic, glottic, or infra-glottic region.",
            "status": "normal",
            "subsites_evaluated": ["supraglottic", "glottic", "infraglottic"],
        },
        "oropharynx": {
            "finding": "Oropharynx and parapharyngeal spaces are normal.",
            "status": "normal",
            "parapharyngeal_spaces_status": "normal",
        },
        "laryngeal_structures": {
            "finding": "Laryngeal, arytenoid, and cricoid cartilages are normal.",
            "status": "normal",
            "structures_evaluated": [
                "laryngeal_cartilage",
                "arytenoid_cartilage",
                "cricoid_cartilage",
            ],
        },
        "thyroid_gland": {
            "finding": "Thyroid gland diffusely enlarged with a large cystic nodule containing septations.",
            "status": "diffusely_enlarged",
            "nodules": [{"size_mm": None, "cystic": True, "septated": True, "location": None}],
        },
        "nasal_septum": {
            "finding": "Nasal septum deviated toward the right.",
            "status": "abnormal",
            "deviation": "right",
        },
    },
    "longitudinal_comparison": {
        "trend": "regression",
        "narrative": (
            "Compared with the prior CT, right-sided cervical lymphadenopathy shows "
            "regression in size and number; no residual or recurrent parotid mass."
        ),
    },
    "conclusion": [
        "No evidence of residual or recurrent mass in the right parotid region.",
        "Small sub-centimeter lymph nodes are seen in the right side of the neck, "
        "showing regression in size and number.",
        "Small sub-centimeter lymph nodes are also seen in the left side of the neck.",
        "In comparison with the previous CT scan, there is regression noted in the "
        "right-sided lymph nodes.",
    ],
    "overall_impression": (
        "Post-treatment right parotid carcinoma with regressing right cervical "
        "lymphadenopathy; no residual/recurrent mass."
    ),
    "confidence": "standard",
}


# ── Gemini response_schema (OpenAPI-subset — NOT full JSON-Schema) ─────────────
# A reduced mirror of the LLM-derived subset of outputs_schema.json. Deliberately
# excludes pipeline-computed fields (qa_flags, qa_details, model_version,
# model_checksum, artifacts) — Gemini should never be asked to produce those.
# Cannot reuse outputs_schema.json verbatim: this SDK's response_schema doesn't
# support "$ref", "additionalProperties", or ["type", "null"] unions.


def _text_status_schema(
    status_enum: list[str],
    extra_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    properties = {
        "finding": {"type": "string"},
        "status": {"type": "string", "enum": status_enum},
    }
    if extra_properties:
        properties.update(extra_properties)
    return {
        "type": "object",
        "properties": properties,
        "required": ["finding", "status"],
    }


_LYMPH_NODE_SCHEMA = _text_status_schema(
    ["normal", "subcentimeter_nodes", "enlarged_nodes", "not_evaluated"],
    extra_properties={
        "count": {"type": "integer", "nullable": True},
        "largest_short_axis_mm": {"type": "number", "nullable": True},
        "comparison_to_baseline": {
            "type": "string",
            "enum": [
                "regression_in_size_and_number",
                "stable",
                "progression",
                "new",
                "resolved",
                "not_applicable",
            ],
        },
    },
)

_GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "object",
            "properties": {
                "right_parotid_region": _text_status_schema(
                    [
                        "no_residual_recurrent_mass",
                        "residual_mass",
                        "recurrent_mass",
                        "indeterminate",
                        "not_evaluated",
                    ],
                    extra_properties={
                        "post_treatment_state": {
                            "type": "string",
                            "enum": [
                                "post_surgery",
                                "post_radiation",
                                "post_surgery_and_radiation",
                                "none",
                                "unknown",
                            ],
                        },
                        "comparison_to_baseline": {
                            "type": "string",
                            "enum": [
                                "stable",
                                "regressed",
                                "progressed",
                                "new",
                                "resolved",
                                "not_applicable",
                            ],
                        },
                    },
                ),
                "lymphnodes_right_neck": _LYMPH_NODE_SCHEMA,
                "lymphnodes_left_neck": _LYMPH_NODE_SCHEMA,
                "mandible_and_maxilla": _text_status_schema(
                    ["normal", "erosion", "destruction", "indeterminate", "not_evaluated"]
                ),
                "supra_glottic_region": _text_status_schema(
                    ["normal", "mass_present", "indeterminate", "not_evaluated"],
                    extra_properties={
                        "subsites_evaluated": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["supraglottic", "glottic", "infraglottic"],
                            },
                        },
                    },
                ),
                "oropharynx": _text_status_schema(
                    ["normal", "abnormal", "indeterminate", "not_evaluated"],
                    extra_properties={
                        "parapharyngeal_spaces_status": {
                            "type": "string",
                            "enum": ["normal", "abnormal", "indeterminate", "not_evaluated"],
                        },
                    },
                ),
                "laryngeal_structures": _text_status_schema(
                    ["normal", "abnormal", "indeterminate", "not_evaluated"],
                    extra_properties={
                        "structures_evaluated": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "laryngeal_cartilage",
                                    "arytenoid_cartilage",
                                    "cricoid_cartilage",
                                ],
                            },
                        },
                    },
                ),
                "thyroid_gland": _text_status_schema(
                    [
                        "normal",
                        "diffusely_enlarged",
                        "focal_nodule",
                        "multinodular",
                        "indeterminate",
                        "not_evaluated",
                    ],
                    extra_properties={
                        "nodules": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "size_mm": {"type": "number", "nullable": True},
                                    "cystic": {"type": "boolean"},
                                    "septated": {"type": "boolean"},
                                    "location": {"type": "string", "nullable": True},
                                },
                            },
                        },
                    },
                ),
                "nasal_septum": _text_status_schema(
                    ["normal", "abnormal", "indeterminate", "not_evaluated"],
                    extra_properties={
                        "deviation": {
                            "type": "string",
                            "enum": ["none", "left", "right", "s_shaped", "indeterminate"],
                        },
                    },
                ),
            },
            "required": list(_REGION_KEYS),
        },
        "longitudinal_comparison": {
            "type": "object",
            "properties": {
                "trend": {
                    "type": "string",
                    "enum": ["progression", "regression", "stable", "mixed", "not_applicable"],
                },
                "narrative": {"type": "string"},
            },
            "required": ["trend", "narrative"],
        },
        "conclusion": {"type": "array", "items": {"type": "string"}},
        "overall_impression": {"type": "string"},
        "confidence": {"type": "string", "enum": ["standard", "low"]},
        "confidence_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "findings",
        "longitudinal_comparison",
        "conclusion",
        "overall_impression",
        "confidence",
    ],
}


# ── DICOM / series helpers ──────────────────────────────────────────────────────


def _select_ct_series(series: list[Series], patterns: list[str]) -> Series | None:
    candidates = []
    for s in series:
        desc = s.series_description or ""
        modality = (s.modality or "").upper()
        if modality == "CT" or any(re.search(p, desc) for p in patterns):
            candidates.append(s)
    if not candidates:
        return None
    # Prefer the series with the most instances (diagnostic volume over a
    # single-slice scout/localizer), matching the _classify_series convention
    # used by pet_ct / pet_ct_brain.
    return max(candidates, key=lambda s: s.num_instances or 0)


def _extract_clinical_context(dicom_path: str) -> tuple[str, str | None, str | None]:
    """Best-effort clinical-history text + demographics straight from the DICOM header."""
    try:
        ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)
    except Exception as exc:
        logger.warning("ct_face_neck_dicom_header_read_failed", error=str(exc))
        return "", None, None

    texts = [str(getattr(ds, tag, "") or "").strip() for tag in _CLINICAL_TEXT_TAGS]
    clinical_indication = " ".join(t for t in texts if t)
    patient_age = str(getattr(ds, "PatientAge", "") or "").strip() or None
    patient_sex = str(getattr(ds, "PatientSex", "") or "").strip() or None
    return clinical_indication, patient_age, patient_sex


def _detect_comparison_hint(clinical_text: str) -> tuple[bool, str | None]:
    """Text-only detection of a prior-study reference. Not an image-based comparison
    — see the module docstring for why real cross-visit trending happens elsewhere.
    """
    if not clinical_text or not _COMPARISON_MENTION_RE.search(clinical_text):
        return False, None
    m = _COMPARISON_DATE_RE.search(clinical_text)
    return True, (m.group(1) if m else None)


def _load_hu_volume(dicom_dir: str) -> tuple[np.ndarray, tuple[float, float, float]]:
    """Build a Hounsfield-unit volume (z, y, x) from a directory of CT DICOMs."""
    reader = sitk.ImageSeriesReader()
    series_ids = reader.GetGDCMSeriesIDs(dicom_dir)
    if series_ids:
        file_names = reader.GetGDCMSeriesFileNames(dicom_dir, series_ids[0])
    else:
        file_names = sorted(str(p) for p in Path(dicom_dir).glob("*.dcm"))
    if not file_names:
        raise ValueError(f"No DICOM files found in {dicom_dir}")

    reader.SetFileNames(file_names)
    img = reader.Execute()
    arr = sitk.GetArrayFromImage(img).astype(np.float32)  # GDCM applies rescale slope/intercept
    return arr, img.GetSpacing()


def _apply_window(slice_hu: np.ndarray, center: float, width: float) -> np.ndarray:
    lower, upper = center - width / 2.0, center + width / 2.0
    clipped = np.clip(slice_hu, lower, upper)
    normed = (clipped - lower) / (upper - lower) if upper > lower else np.zeros_like(clipped)
    return (normed * 255.0).astype(np.uint8)


def _render_preview_slices(
    volume_hu: np.ndarray,
    window_presets: dict[str, dict[str, float]],
    output_dir: str,
    max_total: int,
) -> list[dict[str, Any]]:
    """Render evenly-spaced axial cross-sections in each configured HU window."""
    z_size = volume_hu.shape[0]
    if z_size == 0 or not window_presets:
        return []

    n_windows = len(window_presets)
    n_levels = max(1, max_total // n_windows)

    # Inset 5% from each end — the extreme slices are often mostly air/table.
    inset = max(1, int(z_size * 0.05))
    lo, hi = inset, max(inset + 1, z_size - inset)
    if n_levels == 1 or hi <= lo:
        z_indices = [z_size // 2]
    else:
        z_indices = sorted(
            {
                min(max(int(round(lo + i * (hi - lo) / (n_levels - 1))), 0), z_size - 1)
                for i in range(n_levels)
            }
        )

    os.makedirs(output_dir, exist_ok=True)
    previews: list[dict[str, Any]] = []
    for z in z_indices:
        slice_hu = volume_hu[z, :, :]
        for window_name, params in window_presets.items():
            try:
                uint8 = _apply_window(slice_hu, params["center"], params["width"])
                path = os.path.join(output_dir, f"slice_{z:04d}_{window_name}.png")
                Image.fromarray(uint8, mode="L").save(path, format="PNG")
                previews.append({"path": path, "z_index": z, "window": window_name})
            except Exception as exc:
                logger.warning(
                    "ct_face_neck_preview_render_failed", z=z, window=window_name, error=str(exc)
                )
    return previews


# ── Prompt construction ──────────────────────────────────────────────────────


def _build_prompt(preprocessed: dict[str, Any]) -> str:
    age = preprocessed.get("patient_age") or "unknown"
    sex = preprocessed.get("patient_sex") or "unknown"
    clinical_indication = (
        preprocessed.get("clinical_indication") or "Not recorded in DICOM metadata."
    )
    study_description = preprocessed.get("study_description") or "CT Face & Neck"
    comparison_mentioned = preprocessed.get("comparison_mentioned")
    baseline_hint = preprocessed.get("baseline_date_hint")

    if comparison_mentioned:
        date_clause = f" dated {baseline_hint}." if baseline_hint else " (date not stated)."
        comparison_note = (
            "The clinical history text references a comparison against a prior CT"
            + date_clause
            + " You do NOT have the prior study's images — only describe what is visible in "
            "the CURRENT images provided. Only set a comparison_to_baseline value other than "
            "'not_applicable', or a longitudinal_comparison.trend other than 'not_applicable', "
            "if the clinical text itself states the comparative outcome; never infer a size "
            "change you cannot actually see."
        )
    else:
        comparison_note = (
            "No prior-study reference was found in the clinical history text. Set every "
            "comparison_to_baseline field to 'not_applicable' and longitudinal_comparison.trend "
            "to 'not_applicable' — do not fabricate a comparison."
        )

    return f"""\
You are an expert head & neck radiologist performing structured, per-region finding
extraction from a {study_description} CT study for a {age}-year-old {sex} patient.

CLINICAL HISTORY (from DICOM metadata): {clinical_indication}

{comparison_note}

You are given axial CT slices spanning the face and neck. Each anatomical level is
rendered twice: once in a soft-tissue window (center 40 / width 400 HU) and once in
a bone window (center 700 / width 3000 HU), so both soft-tissue and osseous structures
are visible.

TASK: Evaluate exactly these 9 anatomical regions and report a structured finding for
each, following the reporting convention illustrated in the EXAMPLE below (a
de-identified reference case — use it ONLY to calibrate terminology, granularity, and
enum vocabulary; do NOT copy its findings, they describe a different patient):
  - right_parotid_region  (surgical bed / mass recurrence check)
  - lymphnodes_right_neck (subcentimeter vs. enlarged: >=10mm short axis)
  - lymphnodes_left_neck  (same criteria)
  - mandible_and_maxilla  (bone destruction/erosion)
  - supra_glottic_region  (mass in supraglottic/glottic/infraglottic airway)
  - oropharynx            (oropharynx + parapharyngeal spaces)
  - laryngeal_structures  (laryngeal/arytenoid/cricoid cartilages)
  - thyroid_gland         (size, nodules, cystic/septated features)
  - nasal_septum          (midline position / deviation direction)

EXAMPLE (de-identified reference case — {_FEW_SHOT_CLINICAL_CONTEXT}):
Example structured output:
{json.dumps(_FEW_SHOT_OUTPUT, indent=2)}

RULES:
- Use "not_evaluated" for a region not visible in the provided slices.
- Use "indeterminate" rather than guessing when image quality prevents a confident call.
- Only fill numeric fields (count, largest_short_axis_mm, size_mm) with a genuine visual
  estimate; otherwise use null. These are AI visual estimates, not calibrated caliper
  measurements — never state false precision.
- conclusion must be a short ordered list of plain-English sentences summarising the
  overall read, in the same style as the example.
- Respond ONLY with JSON conforming to the provided response schema.
"""


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """Defensive JSON parse — response_schema + response_mime_type should already
    produce clean JSON, but LLM output is never trusted blindly in this codebase.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _default_extraction() -> dict[str, Any]:
    """Low-confidence placeholder used when Gemini is disabled/unreachable/fails.
    Never blocks the pipeline — matches the mammography/vlm_qa "degrade gracefully" pattern.
    """

    def region(status: str = "not_evaluated", **extra: Any) -> dict[str, Any]:
        return {
            "finding": "Not evaluated — VLM extraction unavailable for this run.",
            "status": status,
            **extra,
        }

    return {
        "findings": {
            "right_parotid_region": region(
                post_treatment_state="unknown", comparison_to_baseline="not_applicable"
            ),
            "lymphnodes_right_neck": region(
                count=None, largest_short_axis_mm=None, comparison_to_baseline="not_applicable"
            ),
            "lymphnodes_left_neck": region(
                count=None, largest_short_axis_mm=None, comparison_to_baseline="not_applicable"
            ),
            "mandible_and_maxilla": region(),
            "supra_glottic_region": region(subsites_evaluated=[]),
            "oropharynx": region(parapharyngeal_spaces_status="not_evaluated"),
            "laryngeal_structures": region(structures_evaluated=[]),
            "thyroid_gland": region(nodules=[]),
            "nasal_septum": region(deviation="indeterminate"),
        },
        "longitudinal_comparison": {"trend": "not_applicable", "narrative": ""},
        "conclusion": [
            "AI structured extraction unavailable for this study; radiologist review required."
        ],
        "overall_impression": "AI structured extraction unavailable.",
        "confidence": "low",
        "confidence_reasons": ["vlm_unavailable"],
    }


def _schema_checksum() -> str:
    """Stable identifier for the prompt/schema contract in lieu of model weights."""
    payload = json.dumps(_GEMINI_RESPONSE_SCHEMA, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _call_gemini_with_retry(
    loop: asyncio.AbstractEventLoop,
    client: GeminiClient,
    prompt: str,
    images: list[bytes],
    *,
    max_output_tokens: int,
    temperature: float,
    attempts: int,
    backoff_seconds: float,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return loop.run_until_complete(
                client.generate_structured_from_images(
                    prompt,
                    images,
                    _GEMINI_RESPONSE_SCHEMA,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
            )
        except Exception as exc:  # noqa: BLE001 — retried, then re-raised for the caller to flag
            last_exc = exc
            logger.warning("ct_face_neck_vlm_attempt_failed", attempt=attempt, error=str(exc))
            if attempt < attempts:
                time.sleep(backoff_seconds)
    assert last_exc is not None
    raise last_exc


# ── Pipeline ──────────────────────────────────────────────────────────────────


class Pipeline(BasePipeline):
    """CT Face & Neck pipeline — Gemini VLM structured per-region extraction."""

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._cfg = yaml.safe_load(f)

    # ── Phase 1: Preprocess ───────────────────────────────────────────────────

    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        loop = event_loop or asyncio.get_event_loop()
        qa_flags: list[str] = []
        qa_details: dict[str, Any] = {}

        cfg_pre = self._cfg.get("preprocessing", {})
        ct_patterns = cfg_pre.get("ct_series_patterns", [r"(?i)\bct\b"])
        target_series = _select_ct_series(series, ct_patterns)
        if target_series is None:
            raise ValueError("No CT series found for ct_face_neck pipeline")

        # Synchronized context loop helper: preprocess() is a sync method, but
        # PACSClient is async (httpx.AsyncClient) — drive it via the event loop
        # tasks.py created and passed in, exactly like every other pipeline.
        dicom_dir = os.path.join(working_dir, "dicoms")
        os.makedirs(dicom_dir, exist_ok=True)
        dicom_paths: list[str] = loop.run_until_complete(
            pacs.download_series_dicoms(
                study.study_instance_uid,
                target_series.series_instance_uid,
                dicom_dir,
            )
        )
        if not dicom_paths:
            raise ValueError("Failed to download CT DICOM files")

        cfg_qc = self._cfg.get("quality_checks", {})
        if len(dicom_paths) < cfg_qc.get("min_slices_for_extraction", 20):
            qa_flags.append("insufficient_slices")
            qa_details["slice_count"] = len(dicom_paths)

        max_thickness = cfg_qc.get("max_slice_thickness_mm", 5.0)
        if target_series.slice_thickness and target_series.slice_thickness > max_thickness:
            qa_flags.append("excessive_slice_thickness")
            qa_details["slice_thickness_mm"] = target_series.slice_thickness

        clinical_indication, patient_age, patient_sex = _extract_clinical_context(dicom_paths[0])
        comparison_mentioned, baseline_date_hint = _detect_comparison_hint(clinical_indication)

        volume_hu, spacing = _load_hu_volume(dicom_dir)

        preview_dir = os.path.join(working_dir, "previews")
        window_presets = cfg_pre.get(
            "window_presets",
            {"soft_tissue": {"center": 40, "width": 400}, "bone": {"center": 700, "width": 3000}},
        )
        max_slices = self._cfg.get("inference", {}).get("vlm", {}).get("max_slices_per_series", 24)
        previews = _render_preview_slices(volume_hu, window_presets, preview_dir, max_slices)
        if not previews:
            qa_flags.append("preview_render_failed")

        logger.info(
            "ct_face_neck_preprocess_complete",
            study_uid=study.study_instance_uid,
            series_uid=target_series.series_instance_uid,
            n_dicoms=len(dicom_paths),
            n_previews=len(previews),
            qa_flags=qa_flags,
        )

        return {
            "study_uid": study.study_instance_uid,
            "series_uid": target_series.series_instance_uid,
            "study_description": study.study_description or "",
            "clinical_indication": clinical_indication,
            "patient_age": patient_age,
            "patient_sex": patient_sex,
            "comparison_mentioned": comparison_mentioned,
            "baseline_date_hint": baseline_date_hint,
            "preview_meta": previews,
            "volume_shape": [int(d) for d in volume_hu.shape],
            "voxel_spacing_mm": [float(v) for v in spacing],
            "qa_flags": qa_flags,
            "qa_details": qa_details,
        }

    # ── Phase 2: Infer ────────────────────────────────────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        qa_flags: list[str] = list(preprocessed.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(preprocessed.get("qa_details", {}))
        preview_paths = [m["path"] for m in preprocessed.get("preview_meta", [])]

        settings = get_settings()
        cfg_vlm = self._cfg.get("inference", {}).get("vlm", {})
        extraction: dict[str, Any] | None = None

        if not settings.llm_enabled or not settings.gemini_api_key:
            logger.warning("ct_face_neck_vlm_disabled", llm_enabled=settings.llm_enabled)
            qa_flags.append("vlm_unavailable")
        elif not preview_paths:
            logger.warning("ct_face_neck_no_previews_available")
            qa_flags.append("vlm_unavailable")
        else:
            client = GeminiClient(api_key=settings.gemini_api_key, model_name=settings.gemini_model)
            if not client.ready:
                qa_flags.append("vlm_unavailable")
            else:
                prompt = _build_prompt(preprocessed)
                images = [Path(p).read_bytes() for p in preview_paths]
                try:
                    loop = asyncio.get_event_loop()
                    raw = _call_gemini_with_retry(
                        loop,
                        client,
                        prompt,
                        images,
                        max_output_tokens=cfg_vlm.get("max_output_tokens", 4096),
                        temperature=cfg_vlm.get("temperature", 0.1),
                        attempts=cfg_vlm.get("retry_attempts", 2),
                        backoff_seconds=cfg_vlm.get("retry_backoff_seconds", 2),
                    )
                    extraction = _parse_json_response(raw)
                except Exception as exc:
                    # Non-blocking: an LLM failure degrades this run to a
                    # low-confidence placeholder rather than failing the job.
                    logger.warning("ct_face_neck_vlm_call_failed", error=str(exc))
                    qa_details["vlm_error"] = str(exc)
                    qa_flags.append("vlm_unavailable")

        if extraction is None:
            extraction = _default_extraction()
            if "low_confidence_extraction" not in qa_flags:
                qa_flags.append("low_confidence_extraction")

        return {
            **{k: v for k, v in preprocessed.items() if k not in ("qa_flags", "qa_details")},
            "extraction": extraction,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
        }

    # ── Phase 3: Postprocess ──────────────────────────────────────────────────

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        extraction = inference_output.get("extraction") or _default_extraction()
        findings = extraction.get("findings", {})
        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        comparison_mentioned = bool(inference_output.get("comparison_mentioned"))
        baseline_hint = inference_output.get("baseline_date_hint")
        longitudinal = extraction.get(
            "longitudinal_comparison", {"trend": "not_applicable", "narrative": ""}
        )

        report_path = os.path.join(artifacts_dir, "extraction.json")
        with open(report_path, "w") as f:
            json.dump(extraction, f, indent=2)

        artifacts: list[dict[str, Any]] = [
            {
                "name": "extraction",
                "artifact_type": "report_json",
                "local_path": os.path.abspath(report_path),
                "content_type": "application/json",
            }
        ]
        for meta in inference_output.get("preview_meta", []):
            path = meta.get("path")
            if path and os.path.exists(path):
                artifacts.append(
                    {
                        "name": os.path.basename(path),
                        "artifact_type": "ct_preview_png",
                        "local_path": os.path.abspath(path),
                        "content_type": "image/png",
                    }
                )

        confidence = extraction.get("confidence", "low")
        confidence_reasons = list(extraction.get("confidence_reasons", []))
        if "vlm_unavailable" in qa_flags and "vlm_unavailable" not in confidence_reasons:
            confidence_reasons.append("vlm_unavailable")
            confidence = "low"

        result = {
            "summary": {
                "study_description": inference_output.get("study_description", ""),
                "body_parts_evaluated": ["HEAD", "NECK"],
                "clinical_indication": inference_output.get("clinical_indication", ""),
                "comparison_performed": comparison_mentioned,
                "baseline_study_date": baseline_hint,
                "overall_impression": extraction.get("overall_impression", ""),
                "conclusion": extraction.get("conclusion", []),
                "confidence": confidence,
                "confidence_reasons": confidence_reasons,
                "processing_notes": (
                    "Findings extracted via Gemini VLM structured extraction (no local "
                    "segmentation model). AI-assisted draft — requires radiologist verification "
                    "before clinical use."
                ),
            },
            "measurements": {
                "findings": findings,
                # region_changes is intentionally always empty here: this pipeline has no
                # access to a prior study's images (see module docstring). Genuine
                # cross-visit numeric trending is computed generically by
                # infrastructure/queue/tasks.py's LongitudinalAnalysisService from prior
                # stored `measurements` and written to summary.longitudinal_analysis.
                "longitudinal_comparison": {
                    "comparison_performed": comparison_mentioned,
                    "baseline_study_date": baseline_hint,
                    "baseline_study_uid": None,
                    "trend": longitudinal.get("trend", "not_applicable"),
                    "region_changes": [],
                    "narrative": longitudinal.get("narrative", ""),
                },
                "voxel_spacing_mm": inference_output.get("voxel_spacing_mm", [0.0, 0.0, 0.0]),
                "image_dimensions": inference_output.get("volume_shape", [0, 0, 0]),
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": "ct_face_neck_gemini_vlm_v1.0.0",
            "model_checksum": _schema_checksum(),
            "artifacts": artifacts,
        }

        logger.info(
            "ct_face_neck_postprocess_complete",
            qa_flags=qa_flags,
            confidence=confidence,
            n_artifacts=len(artifacts),
        )
        return result
