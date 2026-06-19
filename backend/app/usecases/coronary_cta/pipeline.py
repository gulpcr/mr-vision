"""Coronary CT angiography (CCTA) pipeline.

Phases:
  preprocess  — classify the contrast CCTA and non-contrast calcium-score
                series, download each as NIfTI, run input QA.
  infer       — compute the Agatston coronary-artery-calcium score from the
                non-contrast series (deterministic); attempt DL lumen
                segmentation + per-segment stenosis grading when a model is
                loaded (currently stubbed → Agatston-only fallback).
  postprocess — derive calcium category / CAD-RADS, build the result dict,
                render calcium-overlay PNGs and a segmentation NIfTI.

Inference modes (selected automatically):
  dl_stenosis  — learned coronary lumen segmentation + stenosis grading, used
                 when ``model.custom_ccta_weights_path`` is set AND the lumen
                 segmentation method is implemented. NOT YET IMPLEMENTED.
  calcium_only — deterministic Agatston scoring; no stenosis / CAD-RADS.
                 Always available and used as the fallback.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import structlog
import yaml
from scipy import ndimage

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.usecases.base import BasePipeline

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

# SCCT 18-segment coronary model — used when DL stenosis grading is available.
SCCT_SEGMENTS: list[tuple[str, str]] = [
    ("Segment 1 — proximal RCA", "RCA"),
    ("Segment 2 — mid RCA", "RCA"),
    ("Segment 3 — distal RCA", "RCA"),
    ("Segment 4 — PDA", "RCA"),
    ("Segment 5 — left main", "LM"),
    ("Segment 6 — proximal LAD", "LAD"),
    ("Segment 7 — mid LAD", "LAD"),
    ("Segment 8 — distal LAD", "LAD"),
    ("Segment 9 — first diagonal", "LAD"),
    ("Segment 10 — second diagonal", "LAD"),
    ("Segment 11 — proximal LCx", "LCx"),
    ("Segment 12 — first obtuse marginal", "LCx"),
    ("Segment 13 — mid/distal LCx", "LCx"),
    ("Segment 14 — second obtuse marginal", "LCx"),
    ("Segment 15 — posterolateral", "LCx"),
    ("Segment 16 — PLB", "RCA"),
    ("Segment 17 — ramus intermedius", "LM"),
    ("Segment 18 — left main bifurcation", "LM"),
]

# Series-description heuristics
_CALCIUM_PATTERNS = [
    r"(?i)calcium", r"(?i)agatston", r"(?i)\bcac\b",
    r"(?i)ca.*scor", r"(?i)score", r"(?i)non.?contrast",
]
_CCTA_PATTERNS = [
    r"(?i)coronary", r"(?i)ccta", r"(?i)arterial",
    r"(?i)cardiac.*ct", r"(?i)contrast",
]


# ── Agatston scoring helpers ────────────────────────────────────────────────────

def _agatston_weight(peak_hu: float) -> int:
    """Standard Agatston density weighting factor from a lesion's peak HU."""
    if peak_hu >= 400:
        return 4
    if peak_hu >= 300:
        return 3
    if peak_hu >= 200:
        return 2
    if peak_hu >= 130:
        return 1
    return 0


def _calcium_category(score: float) -> str:
    """Map a total Agatston score to a clinical burden category."""
    if score <= 0:
        return "Zero (no detectable calcium)"
    if score <= 10:
        return "Minimal"
    if score <= 100:
        return "Mild"
    if score <= 400:
        return "Moderate"
    return "Severe"


def _cad_rads_from_stenosis(max_stenosis_pct: float) -> int:
    """Derive a CAD-RADS 2.0 category from the worst per-segment stenosis."""
    if max_stenosis_pct <= 0:
        return 0
    if max_stenosis_pct < 25:
        return 1
    if max_stenosis_pct < 50:
        return 2
    if max_stenosis_pct < 70:
        return 3
    if max_stenosis_pct < 100:
        return 4
    return 5


def _build_cardiac_roi(shape: tuple[int, int, int], cfg: dict) -> np.ndarray:
    """Boolean ROI (True = inside heart bounding box) as configured fractions.

    Crude central-thorax box standing in for a learned heart mask. Restricting
    HU>=130 detection to this box keeps sternum/spine/descending-aorta calcium
    from dominating the score, but it both over- and under-counts at the margins
    — hence the calcium_roi_approximate QA flag.
    """
    x, y, z = shape
    roi = np.zeros(shape, dtype=bool)
    xl, xh = int(x * cfg.get("roi_x_lo", 0.30)), int(x * cfg.get("roi_x_hi", 0.80))
    yl, yh = int(y * cfg.get("roi_y_lo", 0.25)), int(y * cfg.get("roi_y_hi", 0.78))
    zl, zh = int(z * cfg.get("roi_z_lo", 0.10)), int(z * cfg.get("roi_z_hi", 0.90))
    roi[xl:xh, yl:yh, zl:zh] = True
    return roi


def _compute_agatston(
    hu_arr: np.ndarray,
    voxel_spacing_mm: tuple[float, float, float],
    cfg: dict,
    roi: np.ndarray | None = None,
) -> dict[str, Any]:
    """Compute the Agatston coronary-calcium score over axial slices.

    Iterates the last (Z) axis, labels 2D HU>=threshold components per slice
    within the cardiac ROI, weights each by peak HU, and scales the total by
    actual/reference slice thickness. Returns the score, calcium volume, lesion
    count, and a binary calcium mask.

    ``roi`` is an optional boolean array (True = inside the cardiac ROI) aligned
    with ``hu_arr``; when omitted the heuristic bounding box is used. The
    detection threshold, weighting, and thickness correction are identical
    regardless of which ROI is supplied — only the spatial restriction changes.
    """
    hu_threshold = cfg.get("hu_threshold", 130)
    min_area_mm2 = cfg.get("min_lesion_area_mm2", 1.0)
    ref_thickness = cfg.get("reference_slice_thickness_mm", 3.0)

    sx, sy, sz = voxel_spacing_mm
    pixel_area_mm2 = sx * sy
    voxel_vol_mm3 = sx * sy * sz

    if roi is None:
        roi = _build_cardiac_roi(hu_arr.shape, cfg)
    candidate = (hu_arr >= hu_threshold) & roi

    calcium_mask = np.zeros(hu_arr.shape, dtype=np.uint8)
    total_score = 0.0
    total_volume_mm3 = 0.0
    lesion_count = 0

    n_slices = hu_arr.shape[2]
    for z in range(n_slices):
        slice_mask = candidate[:, :, z]
        if not slice_mask.any():
            continue
        labeled, n = ndimage.label(slice_mask)
        slice_hu = hu_arr[:, :, z]
        for comp_id in range(1, n + 1):
            comp = labeled == comp_id
            area_mm2 = float(comp.sum()) * pixel_area_mm2
            if area_mm2 < min_area_mm2:
                continue
            peak_hu = float(slice_hu[comp].max())
            weight = _agatston_weight(peak_hu)
            if weight == 0:
                continue
            total_score += area_mm2 * weight
            total_volume_mm3 += float(comp.sum()) * voxel_vol_mm3
            lesion_count += 1
            calcium_mask[:, :, z][comp] = 1

    # Scale to the Agatston reference slice thickness (defined at ~3 mm).
    thickness_correction = sz / ref_thickness if ref_thickness > 0 else 1.0
    total_score *= thickness_correction

    return {
        "agatston_score": round(total_score, 1),
        "calcium_volume_mm3": round(total_volume_mm3, 1),
        "lesion_count": lesion_count,
        "calcium_mask": calcium_mask,
        "thickness_correction": round(thickness_correction, 3),
    }


def _generate_calcium_overlay_pngs(
    hu_arr: np.ndarray,
    calcium_mask: np.ndarray,
    output_dir: str,
    cfg: dict,
) -> list[dict]:
    """Render the top-N axial slices (by calcium area) with calcium highlighted."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_overlay")
        return []

    os.makedirs(output_dir, exist_ok=True)
    wc = cfg.get("hu_window_center", 300)
    ww = cfg.get("hu_window_width", 1500)
    vmin, vmax = wc - ww / 2.0, wc + ww / 2.0
    top_n = cfg.get("overlay_top_n_slices", 3)

    per_slice_area = calcium_mask.sum(axis=(0, 1))
    candidate_slices = np.argsort(per_slice_area)[::-1]
    candidate_slices = [int(z) for z in candidate_slices if per_slice_area[z] > 0][:top_n]

    artifacts: list[dict] = []
    for rank, z in enumerate(candidate_slices):
        try:
            hu_slice = hu_arr[:, :, z].T
            mask_slice = calcium_mask[:, :, z].T
            fig, ax = plt.subplots(figsize=(6, 6), facecolor="black")
            ax.imshow(hu_slice, cmap="gray", vmin=vmin, vmax=vmax, origin="lower")
            overlay = np.ma.masked_where(mask_slice == 0, mask_slice)
            ax.imshow(overlay, cmap="autumn", alpha=0.7, origin="lower", vmin=0, vmax=1)
            ax.axis("off")
            ax.set_title(f"Calcium overlay — slice {z}", color="white", fontsize=9, pad=4)
            png_path = os.path.join(output_dir, f"calcium_overlay_{rank}.png")
            fig.savefig(png_path, dpi=120, bbox_inches="tight", facecolor="black")
            plt.close(fig)
            artifacts.append({
                "name": f"calcium_overlay_{rank}.png",
                "artifact_type": "overlay_png",
                "local_path": png_path,
                "content_type": "image/png",
            })
        except Exception as exc:
            logger.error("calcium_overlay_failed", slice=z, error=str(exc))
            try:
                plt.close("all")
            except Exception:
                pass
    return artifacts


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Pipeline(BasePipeline):
    """Coronary CTA pipeline — Agatston calcium scoring with DL stenosis stub."""

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._cfg = yaml.safe_load(f)

        self._model = None
        self._device = None
        self._model_version: str = "coronary_cta_agatston_v0.1.0"
        self._model_checksum: str = "n/a_threshold_based"

        weights_path = self._cfg.get("model", {}).get("custom_ccta_weights_path")
        if weights_path:
            try:
                self._load_model(weights_path)
            except Exception as exc:
                logger.warning(
                    "ccta_model_load_failed_using_calcium_only",
                    weights=weights_path,
                    error=str(exc),
                )

    # ── DL model management ──────────────────────────────────────────────────

    def _load_model(self, weights_path: str) -> None:
        """Load coronary lumen-segmentation weights (mirrors pet_ct loader)."""
        import hashlib

        import torch
        from monai.networks.nets import SwinUNETR

        model_cfg = self._cfg.get("model", {})
        inf_cfg = self._cfg.get("inference", {})

        in_channels = model_cfg.get("in_channels", 1)
        out_channels = model_cfg.get("out_channels", 2)
        feature_size = model_cfg.get("feature_size", 48)
        roi_size = tuple(inf_cfg.get("roi_size", [96, 96, 96]))
        use_checkpoint = model_cfg.get("use_checkpoint", False)

        device_str = inf_cfg.get("device", "auto")
        if device_str == "auto":
            device_str = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = torch.device(device_str)

        model = SwinUNETR(
            img_size=roi_size,
            in_channels=in_channels,
            out_channels=out_channels,
            feature_size=feature_size,
            use_checkpoint=use_checkpoint,
        )
        state = torch.load(weights_path, map_location="cpu", weights_only=True)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        elif isinstance(state, dict) and "model" in state:
            state = state["model"]
        model.load_state_dict(state, strict=False)
        model.to(self._device)
        model.eval()
        self._model = model

        with open(weights_path, "rb") as fh:
            sha = hashlib.sha256()
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        self._model_checksum = sha.hexdigest()[:16]
        self._model_version = f"coronary_cta_swinunetr_{Path(weights_path).stem}"
        logger.info("ccta_model_loaded", device=str(self._device), checksum=self._model_checksum)

    def _run_lumen_segmentation(self, ccta_hu: np.ndarray) -> dict[str, Any]:
        """Learned coronary lumen segmentation → centerline → per-segment stenosis.

        NOT YET IMPLEMENTED. Requires: (1) a trained lumen-segmentation model,
        (2) centerline extraction (e.g. skimage.morphology.skeletonize_3d or
        VMTK/kimimaro), (3) curved-MPR cross-sectional lumen-area sampling, and
        (4) per-segment assignment on the SCCT 18-segment model. Until then the
        pipeline runs Agatston-only.
        """
        raise NotImplementedError("coronary lumen segmentation / stenosis grading not yet implemented")

    # ── Cardiac ROI (TotalSegmentator heart mask) ─────────────────────────────

    def _run_heart_roi_totalseg(
        self,
        calcium_nifti_path: str,
        hu_shape: tuple[int, int, int],
        voxel_spacing_mm: tuple[float, float, float],
        working_dir: str,
        cfg: dict,
    ) -> np.ndarray | None:
        """Build the Agatston cardiac ROI from a TotalSegmentator `heart` mask.

        Runs TotalSegmentator on the non-contrast calcium CT (same grid as the
        Agatston HU array), loads the `heart` label, and dilates it outward by
        ``heart_roi_dilate_mm`` so the epicardial coronary arteries — which sit
        just outside the cardiac silhouette — fall inside the ROI.

        Returns a boolean array aligned with ``hu_shape`` (True = inside ROI),
        or None on any failure so the caller falls back to the heuristic box.
        Mirrors the TotalSegmentator usage already proven in the pet_ct pipeline.
        """
        from totalsegmentator.python_api import totalsegmentator as ts_run

        import torch

        task = cfg.get("totalseg_task", "total")
        device = "gpu" if torch.cuda.is_available() else "cpu"
        ts_out = os.path.join(working_dir, "ccta_heart_seg")
        os.makedirs(ts_out, exist_ok=True)

        logger.info("heart_roi_totalseg_start", task=task, device=device)
        ts_run(
            input=Path(calcium_nifti_path),
            output=Path(ts_out),
            task=task,
            device=device,
            quiet=True,
            roi_subset=["heart"],
        )

        heart_path = os.path.join(ts_out, "heart.nii.gz")
        if not os.path.exists(heart_path):
            logger.warning("heart_roi_mask_missing", path=heart_path)
            return None

        heart_mask = nib.load(heart_path).get_fdata() > 0.5
        if heart_mask.shape != tuple(hu_shape):
            logger.warning(
                "heart_roi_shape_mismatch",
                heart_shape=tuple(heart_mask.shape),
                hu_shape=tuple(hu_shape),
            )
            return None
        if not heart_mask.any():
            logger.warning("heart_roi_empty")
            return None

        # Dilate outward to include epicardial coronaries. Convert the mm margin
        # to in-plane voxels (axial calcium scoring is dominated by in-plane
        # geometry); guard against degenerate spacing.
        dilate_mm = float(cfg.get("heart_roi_dilate_mm", 8.0))
        in_plane_spacing = max(float(voxel_spacing_mm[0]), 0.1)
        iterations = int(round(dilate_mm / in_plane_spacing))
        if iterations > 0:
            heart_mask = ndimage.binary_dilation(heart_mask, iterations=iterations)

        logger.info(
            "heart_roi_totalseg_complete",
            roi_voxels=int(heart_mask.sum()),
            dilate_mm=dilate_mm,
            dilate_iterations=iterations,
        )
        return heart_mask

    # ── Series classification ─────────────────────────────────────────────────

    def _classify_series(self, series: list[Series]) -> dict[str, Series | None]:
        """Split CT series into the non-contrast calcium scan and the contrast CCTA.

        Uses series-description patterns first, then slice-thickness as a
        tie-breaker (native calcium scans are thick ~3 mm; CCTA is thin <1 mm).
        """
        calcium: Series | None = None
        ccta: Series | None = None

        for s in series:
            modality = (getattr(s, "modality", "") or "").upper()
            if modality and modality != "CT":
                continue
            desc = (s.series_description or "").strip()
            is_calcium = any(re.search(p, desc) for p in _CALCIUM_PATTERNS)
            is_ccta = any(re.search(p, desc) for p in _CCTA_PATTERNS)

            if is_calcium and not is_ccta and calcium is None:
                calcium = s
            elif is_ccta and not is_calcium and ccta is None:
                ccta = s

        # Slice-thickness tie-breaker for series that matched neither/both.
        unassigned = [
            s for s in series
            if s not in (calcium, ccta)
            and (not getattr(s, "modality", None) or s.modality.upper() == "CT")
        ]
        for s in unassigned:
            thickness = getattr(s, "slice_thickness", None)
            if thickness is None:
                continue
            if thickness >= 2.0 and calcium is None:
                calcium = s
            elif thickness < 2.0 and ccta is None:
                ccta = s

        # Last resort: if nothing classified, treat the first CT series as calcium.
        if calcium is None and ccta is None and series:
            calcium = series[0]

        return {"CALCIUM": calcium, "CCTA": ccta}

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

        classified = self._classify_series(series)
        calcium_series = classified["CALCIUM"]
        ccta_series = classified["CCTA"]

        if calcium_series is None and ccta_series is None:
            raise ValueError("No CT series found for coronary_cta pipeline")

        nifti_dir = os.path.join(working_dir, "nifti")
        os.makedirs(nifti_dir, exist_ok=True)

        calcium_nifti_path = None
        if calcium_series is not None:
            calcium_nifti_path = os.path.join(nifti_dir, "calcium.nii.gz")
            try:
                loop.run_until_complete(
                    pacs.download_series_as_nifti(
                        study.study_instance_uid,
                        calcium_series.series_instance_uid,
                        calcium_nifti_path,
                    )
                )
            except Exception as exc:
                logger.warning("calcium_download_failed", error=str(exc))
                calcium_nifti_path = None
        if calcium_nifti_path is None:
            qa_flags.append("no_calcium_series")
            qa_details["calcium_note"] = (
                "No non-contrast calcium-score series; Agatston score unavailable."
            )

        ccta_nifti_path = None
        if ccta_series is not None:
            ccta_nifti_path = os.path.join(nifti_dir, "ccta.nii.gz")
            try:
                loop.run_until_complete(
                    pacs.download_series_as_nifti(
                        study.study_instance_uid,
                        ccta_series.series_instance_uid,
                        ccta_nifti_path,
                    )
                )
            except Exception as exc:
                logger.warning("ccta_download_failed", error=str(exc))
                ccta_nifti_path = None
        if ccta_nifti_path is None:
            qa_flags.append("no_ccta_series")
            qa_details["ccta_note"] = (
                "No contrast CCTA series; stenosis grading / CAD-RADS unavailable."
            )

        # QA: calcium series slice thickness sanity check
        qc = self._cfg.get("quality_checks", {})
        if calcium_series is not None:
            thickness = getattr(calcium_series, "slice_thickness", None)
            max_thick = qc.get("max_calcium_slice_thickness_mm", 3.5)
            if thickness is not None and thickness > max_thick:
                qa_flags.append("calcium_slice_thickness_abnormal")
                qa_details["calcium_slice_thickness_mm"] = thickness

        logger.info(
            "coronary_cta_preprocess_complete",
            study_uid=study.study_instance_uid,
            has_calcium=calcium_nifti_path is not None,
            has_ccta=ccta_nifti_path is not None,
            qa_flags=qa_flags,
        )

        return {
            "calcium_nifti_path": calcium_nifti_path,
            "ccta_nifti_path": ccta_nifti_path,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    # ── Phase 2: Infer ────────────────────────────────────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("coronary_cta_inference_start")
        calcium_cfg = self._cfg.get("calcium_scoring", {})
        qa_flags: list[str] = list(preprocessed.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(preprocessed.get("qa_details", {}))

        # ── Agatston calcium scoring (deterministic) ──────────────────────────
        agatston: dict[str, Any] = {
            "agatston_score": 0.0,
            "calcium_volume_mm3": 0.0,
            "lesion_count": 0,
            "calcium_mask": None,
        }
        hu_arr = None
        affine = None
        voxel_spacing = (1.0, 1.0, 1.0)

        if preprocessed.get("calcium_nifti_path"):
            cal_img = nib.load(preprocessed["calcium_nifti_path"])
            hu_arr = cal_img.get_fdata().astype(np.float32)
            affine = cal_img.affine
            voxel_spacing = tuple(abs(float(affine[i, i])) for i in range(3))

            qc = self._cfg.get("quality_checks", {})
            if max(hu_arr.shape) < qc.get("min_slices", 30):
                qa_flags.append("insufficient_coverage")
                qa_details["calcium_dimensions"] = list(hu_arr.shape)

            # Prefer an anatomy-aware TotalSegmentator heart-mask ROI; fall back
            # to the heuristic bounding box on any failure (preserving prior
            # behaviour). The Agatston math is identical for both ROIs.
            heart_roi = None
            if calcium_cfg.get("use_totalseg_heart_roi", True):
                try:
                    heart_roi = self._run_heart_roi_totalseg(
                        preprocessed["calcium_nifti_path"],
                        hu_arr.shape,
                        voxel_spacing,
                        working_dir,
                        calcium_cfg,
                    )
                except Exception as exc:
                    logger.warning("heart_roi_totalseg_failed_using_bbox", error=str(exc))
                    heart_roi = None

            agatston = _compute_agatston(hu_arr, voxel_spacing, calcium_cfg, roi=heart_roi)
            if heart_roi is not None:
                qa_details["calcium_roi_method"] = "totalseg_heart_mask"
                qa_details["calcium_roi_dilate_mm"] = calcium_cfg.get("heart_roi_dilate_mm", 8.0)
            else:
                # Heuristic ROI — flag as approximate (no learned heart mask).
                qa_flags.append("calcium_roi_approximate")
                qa_details["calcium_roi_method"] = "heuristic_bbox"
            logger.info(
                "agatston_complete",
                score=agatston["agatston_score"],
                lesions=agatston["lesion_count"],
                roi_method=qa_details["calcium_roi_method"],
            )

        # ── DL stenosis (stub → fallback) ─────────────────────────────────────
        segments: list[dict[str, Any]] = []
        max_stenosis_pct: float | None = None
        inference_method = "calcium_only"

        if self._model is not None and preprocessed.get("ccta_nifti_path"):
            try:
                ccta_img = nib.load(preprocessed["ccta_nifti_path"])
                ccta_hu = ccta_img.get_fdata().astype(np.float32)
                dl_out = self._run_lumen_segmentation(ccta_hu)
                segments = dl_out.get("segments", [])
                max_stenosis_pct = dl_out.get("max_stenosis_pct")
                inference_method = "dl_stenosis"
            except NotImplementedError:
                logger.info("lumen_segmentation_not_implemented_using_calcium_only")
            except Exception as exc:
                logger.warning("dl_stenosis_failed_using_calcium_only", error=str(exc))

        return {
            "agatston": {k: v for k, v in agatston.items() if k != "calcium_mask"},
            "calcium_mask": agatston.get("calcium_mask"),
            "hu_array": hu_arr,
            "affine": affine,
            "voxel_spacing_mm": voxel_spacing,
            "segments": segments,
            "max_stenosis_pct": max_stenosis_pct,
            "inference_method": inference_method,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": preprocessed.get("study_uid"),
        }

    # ── Phase 3: Postprocess ──────────────────────────────────────────────────

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("coronary_cta_postprocess_start")
        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        agatston = inference_output["agatston"]
        calcium_mask: np.ndarray | None = inference_output.get("calcium_mask")
        hu_arr: np.ndarray | None = inference_output.get("hu_array")
        affine = inference_output.get("affine")
        voxel_spacing = inference_output["voxel_spacing_mm"]
        segments: list[dict] = inference_output.get("segments", [])
        max_stenosis_pct = inference_output.get("max_stenosis_pct")
        inference_method = inference_output.get("inference_method", "calcium_only")
        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        score = agatston["agatston_score"]
        category = _calcium_category(score)
        stenosis_available = inference_method == "dl_stenosis"

        cad_rads: int | None = None
        if stenosis_available and max_stenosis_pct is not None:
            cad_rads = _cad_rads_from_stenosis(max_stenosis_pct)

        # Per-vessel calcium attribution requires a learned coronary territory
        # mask; with the heuristic ROI we report total only.
        calcium_per_vessel = {
            "LM": 0.0, "LAD": 0.0, "LCx": 0.0, "RCA": 0.0, "total": score,
        }

        diagnosis = self._derive_diagnosis(score, category, stenosis_available, cad_rads)
        processing_notes = self._build_notes(
            score, category, inference_method, qa_flags, max_stenosis_pct,
            roi_method=qa_details.get("calcium_roi_method"),
        )

        artifacts: list[dict] = []

        # Segmentation NIfTI (so the Phase-7 DICOM Seg export picks it up).
        if calcium_mask is not None and affine is not None:
            seg_path = os.path.join(artifacts_dir, "calcium_mask.nii.gz")
            nib.save(nib.Nifti1Image(calcium_mask, affine), seg_path)
            artifacts.append({
                "name": "calcium_mask",
                "artifact_type": "segmentation_nifti",
                "local_path": seg_path,
                "content_type": "application/gzip",
            })

        # Calcium overlay PNGs
        if (
            calcium_mask is not None
            and hu_arr is not None
            and self._cfg.get("postprocessing", {}).get("generate_overlay", True)
        ):
            artifacts.extend(
                _generate_calcium_overlay_pngs(
                    hu_arr, calcium_mask, artifacts_dir, self._cfg.get("postprocessing", {})
                )
            )

        # Report JSON
        report_path = os.path.join(artifacts_dir, "report.json")
        with open(report_path, "w") as f:
            json.dump(
                {
                    "agatston_score": score,
                    "calcium_category": category,
                    "calcium_per_vessel": calcium_per_vessel,
                    "segments": segments,
                    "cad_rads": cad_rads,
                    "inference_method": inference_method,
                },
                f,
                indent=2,
            )
        artifacts.append({
            "name": "report",
            "artifact_type": "report_json",
            "local_path": report_path,
            "content_type": "application/json",
        })

        result = {
            "summary": {
                "calcium_score_agatston": score,
                "calcium_category": category,
                "calcium_volume_mm3": agatston["calcium_volume_mm3"],
                "calcium_lesion_count": agatston["lesion_count"],
                "stenosis_analysis_available": stenosis_available,
                "max_stenosis_pct": max_stenosis_pct,
                "cad_rads": cad_rads,
                "diagnosis": diagnosis,
                "inference_method": inference_method,
                "processing_notes": processing_notes,
            },
            "measurements": {
                "calcium_per_vessel": calcium_per_vessel,
                "segments": segments,
                "voxel_spacing_mm": [round(s, 3) for s in voxel_spacing],
                "image_dimensions": list(hu_arr.shape) if hu_arr is not None else [],
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": self._model_version,
            "model_checksum": self._model_checksum,
            "artifacts": artifacts,
        }

        logger.info(
            "coronary_cta_postprocess_complete",
            agatston=score,
            category=category,
            cad_rads=cad_rads,
            method=inference_method,
            qa_flags=qa_flags,
        )
        return result

    # ── Narrative helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _derive_diagnosis(
        score: float, category: str, stenosis_available: bool, cad_rads: int | None
    ) -> str:
        base = (
            f"Coronary artery calcium (Agatston) score {score:.0f} — {category} calcified "
            "plaque burden."
        )
        if stenosis_available and cad_rads is not None:
            return f"{base} CAD-RADS {cad_rads}. Clinical correlation recommended."
        return (
            f"{base} Luminal stenosis was not assessed (no contrast lumen analysis); "
            "CAD-RADS not assigned. Clinical correlation recommended."
        )

    @staticmethod
    def _build_notes(
        score: float,
        category: str,
        inference_method: str,
        qa_flags: list[str],
        max_stenosis_pct: float | None,
        roi_method: str | None = None,
    ) -> str:
        parts: list[str] = []
        method_label = {
            "dl_stenosis": "DL coronary lumen segmentation + Agatston calcium scoring",
            "calcium_only": "Agatston calcium scoring (deterministic; no stenosis analysis)",
        }.get(inference_method, inference_method)
        parts.append(f"Method: {method_label}.")

        if "no_calcium_series" in qa_flags:
            parts.append(
                "No non-contrast calcium series available — Agatston score reported as 0 "
                "but is not valid; provide a gated non-contrast scan."
            )
        else:
            parts.append(f"Agatston {score:.0f} ({category}).")

        if roi_method == "totalseg_heart_mask":
            parts.append(
                "Calcium detection was restricted to a TotalSegmentator heart mask "
                "(dilated to include epicardial coronaries), excluding most non-coronary "
                "calcium. Per-vessel attribution still requires a learned coronary "
                "territory map and is not reported."
            )
        elif "calcium_roi_approximate" in qa_flags:
            parts.append(
                "Calcium detection used a heuristic cardiac bounding box rather than a "
                "learned heart mask; per-vessel attribution is not available and the score "
                "may include non-coronary calcium."
            )
        if "calcium_slice_thickness_abnormal" in qa_flags:
            parts.append(
                "Calcium-score series slice thickness is outside the standard range; "
                "Agatston score may be unreliable."
            )
        if inference_method == "calcium_only" and "no_ccta_series" not in qa_flags:
            parts.append("Stenosis grading model not available; CAD-RADS not assigned.")
        return " ".join(parts)
