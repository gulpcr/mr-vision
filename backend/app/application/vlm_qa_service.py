from __future__ import annotations

import glob
import io
import json
import os
import re
from typing import Any

import numpy as np
import structlog

from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

_SLICE_SIZE = 256  # Resize rendered slice to this (pixels) before API call

_VLM_QA_PROMPT = """\
You are an MRI image quality assessment expert. Examine this MRI scan slice carefully.

Respond ONLY with a valid JSON object — no markdown fences, no extra text — using this exact schema:
{
  "quality_score": <integer 1-10, where 10 is perfect quality>,
  "artifacts": ["<artifact_name>", ...],
  "assessment": "<1-2 sentence plain-English summary>"
}

Use ONLY these artifact names (return an empty array if quality is acceptable):
- "motion_artifact"           — blurring, ghosting or smearing from patient movement
- "low_snr"                   — grainy or excessively noisy image
- "field_inhomogeneity"       — uneven signal brightness/intensity shading across the image
- "aliasing_artifact"         — wrap-around or Gibbs ringing at image edges
- "susceptibility_artifact"   — signal void or geometric distortion near metal or air-tissue interfaces
- "truncation_artifact"       — ringing bands at sharp high-contrast boundaries (Gibbs phenomenon)
- "chemical_shift_artifact"   — bright or dark displacement bands at fat-water interfaces
- "parallel_imaging_artifact" — noise amplification or aliasing from SENSE/GRAPPA reconstruction
"""

# Map VLM artifact name → QAFlag string value
_ARTIFACT_FLAG_MAP: dict[str, str] = {
    "motion_artifact": "motion_artifact",
    "low_snr": "low_snr",
    "field_inhomogeneity": "field_inhomogeneity",
    "aliasing_artifact": "aliasing_artifact",
    "susceptibility_artifact": "susceptibility_artifact",
    "truncation_artifact": "truncation_artifact",
    "chemical_shift_artifact": "chemical_shift_artifact",
    "parallel_imaging_artifact": "parallel_imaging_artifact",
}


class VLMQAService:
    """Runs VLM-based image quality assessment on rendered MRI slices."""

    def __init__(self, client: GeminiClient, max_series: int = 3):
        self._client = client
        self._max_series = max_series

    @property
    def available(self) -> bool:
        return self._client.ready

    async def check_working_dir(self, working_dir: str) -> dict[str, Any]:
        """
        Scan working_dir for NIfTI files, render middle axial slices, run VLM QA.

        Returns:
            {
                "flags": list[str],          # QAFlag-compatible strings
                "details": dict[str, Any],   # per-series scores and assessments
            }
        """
        if not self._client.ready:
            return {"flags": [], "details": {}}

        nifti_paths = _find_nifti_files(working_dir)
        if not nifti_paths:
            logger.info("vlm_qa_no_nifti_found", working_dir=working_dir)
            return {"flags": [], "details": {}}

        paths_to_check = nifti_paths[: self._max_series]
        all_flags: list[str] = []
        per_series: dict[str, Any] = {}

        for path in paths_to_check:
            fname = os.path.relpath(path, working_dir)
            try:
                png_bytes = _render_middle_slice(path)
            except Exception as exc:
                logger.warning("vlm_qa_render_failed", file=fname, error=str(exc))
                continue

            try:
                raw = await self._client.generate_from_image(_VLM_QA_PROMPT, png_bytes)
                parsed = _parse_json_response(raw)
            except Exception as exc:
                logger.warning("vlm_qa_api_failed", file=fname, error=str(exc))
                continue

            score = parsed.get("quality_score", 10)
            artifacts: list[str] = parsed.get("artifacts", [])
            assessment: str = parsed.get("assessment", "")

            logger.info(
                "vlm_qa_result",
                file=fname,
                quality_score=score,
                artifacts=artifacts,
            )

            per_series[fname] = {
                "quality_score": score,
                "artifacts": artifacts,
                "assessment": assessment,
            }

            for artifact in artifacts:
                flag = _ARTIFACT_FLAG_MAP.get(artifact, artifact)
                if flag not in all_flags:
                    all_flags.append(flag)

        return {
            "flags": all_flags,
            "details": {
                "vlm_model": "gemini",
                "series_checked": len(per_series),
                "per_series": per_series,
            },
        }


def _find_nifti_files(working_dir: str) -> list[str]:
    """Return NIfTI files sorted by path depth (shallowest first) for consistent ordering."""
    pattern = os.path.join(working_dir, "**", "*.nii.gz")
    files = glob.glob(pattern, recursive=True)
    return sorted(files, key=lambda p: (p.count(os.sep), p))


def _render_middle_slice(nifti_path: str) -> bytes:
    """
    Load a NIfTI volume, extract the middle axial slice, normalise to 0-255,
    resize to _SLICE_SIZE × _SLICE_SIZE, and return PNG bytes.
    """
    import nibabel as nib
    from PIL import Image

    img = nib.load(nifti_path)
    data = np.asarray(img.dataobj, dtype=np.float32)

    # Collapse 4-D to 3-D (first timepoint / channel)
    if data.ndim == 4:
        data = data[..., 0]
    if data.ndim != 3:
        raise ValueError(f"Unexpected NIfTI ndim={data.ndim} in {nifti_path}")

    mid_z = data.shape[2] // 2
    slice_2d = data[:, :, mid_z]

    # Robust percentile normalisation — ignore background zeros
    foreground = slice_2d[slice_2d > 0]
    if foreground.size > 0:
        p1, p99 = np.percentile(foreground, [1, 99])
    else:
        p1, p99 = 0.0, 1.0

    if p99 > p1:
        normed = np.clip((slice_2d - p1) / (p99 - p1), 0.0, 1.0)
    else:
        normed = np.zeros_like(slice_2d)

    uint8 = (normed * 255).astype(np.uint8)
    pil_img = Image.fromarray(uint8, mode="L").resize(
        (_SLICE_SIZE, _SLICE_SIZE), Image.BILINEAR
    )

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_json_response(text: str) -> dict[str, Any]:
    """Parse JSON from VLM response, tolerating surrounding prose and markdown fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = cleaned.find("{", idx)
        if start == -1:
            break
        try:
            obj, _ = decoder.raw_decode(cleaned, start)
            return obj  # type: ignore[return-value]
        except json.JSONDecodeError:
            idx = start + 1
    raise ValueError("No valid JSON object in VLM response")
