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

    def _run_lumen_inference(self, ccta_hu: np.ndarray) -> np.ndarray:
        """Run the loaded SwinUNETR lumen model → binary lumen mask (uint8).

        Z-score-normalises the contrast CCTA over its non-zero voxels (matching
        the other MONAI pipelines) and runs sliding-window inference. With
        out_channels == 1 a sigmoid threshold is applied; otherwise argmax over
        the channel dimension selects the lumen class (label 1).
        """
        import torch
        from monai.inferers import sliding_window_inference

        inf_cfg = self._cfg.get("inference", {})
        model_cfg = self._cfg.get("model", {})

        arr = ccta_hu.astype(np.float32).copy()
        nz = arr != 0
        if np.any(nz):
            m = float(np.mean(arr[nz]))
            s = float(np.std(arr[nz]))
            if s > 0:
                arr = (arr - m) / s
                arr[~nz] = 0.0

        t = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(self._device)
        with torch.no_grad():
            out = sliding_window_inference(
                t,
                tuple(inf_cfg.get("roi_size", [96, 96, 96])),
                inf_cfg.get("sw_batch_size", 1),
                self._model,
                overlap=inf_cfg.get("overlap", 0.5),
                mode=inf_cfg.get("mode", "gaussian"),
            )
        if model_cfg.get("out_channels", 2) == 1:
            prob = torch.sigmoid(out)[0, 0].cpu().numpy()
            mask = (prob >= inf_cfg.get("dl_lumen_threshold", 0.5)).astype(np.uint8)
        else:
            mask = (torch.argmax(out, dim=1)[0].cpu().numpy() == 1).astype(np.uint8)
        return mask

    def _grade_stenosis(self, pct: float) -> str:
        """Severity label for a per-vessel diameter-stenosis percentage."""
        if pct >= 100:
            return "occluded"
        if pct >= 70:
            return "severe"
        if pct >= 50:
            return "moderate"
        if pct >= 25:
            return "mild"
        if pct > 0:
            return "minimal"
        return "none"

    def _vessel_stenosis(
        self,
        vessel_mask: np.ndarray,
        voxel_spacing_mm: tuple[float, float, float],
        scfg: dict,
    ) -> dict[str, Any] | None:
        """Quantify diameter stenosis for a single connected lumen component.

        Method (purely geometric — NO anatomical labelling):
          1. Skeletonise the vessel to a 1-voxel centerline.
          2. Prune skeleton endpoints (which taper to a point and would create a
             false minimum) by iteratively removing voxels with <=1 neighbour.
          3. Sample the local lumen radius at each centerline voxel from the
             Euclidean distance transform (mm, anisotropy-aware), diameter = 2r.
          4. diameter-stenosis % = (D_ref - D_min) / D_ref, where D_ref is a high
             percentile (healthy proximal reference) and D_min a low percentile
             (minimal lumen) of the along-vessel diameter profile.

        Returns None for vessels too small/short to grade reliably.
        """
        from scipy import ndimage as _ndi

        try:
            from skimage.morphology import skeletonize
        except Exception as exc:
            logger.warning("skeletonize_unavailable", error=str(exc))
            return None

        min_vox = int(scfg.get("min_centerline_voxels", 10))
        min_ref_mm = float(scfg.get("min_reference_diameter_mm", 1.5))
        ref_pct = float(scfg.get("reference_percentile", 80))
        min_pct = float(scfg.get("minimal_percentile", 5))
        prune_iters = int(scfg.get("endpoint_prune_iterations", 3))

        # Radius (mm) at every interior voxel = distance to the lumen boundary.
        dt = _ndi.distance_transform_edt(vessel_mask, sampling=voxel_spacing_mm)

        skel = skeletonize(vessel_mask > 0)
        # Prune endpoints: 3x3x3 neighbour count (minus self) <= 1 ⇒ tip.
        kernel = np.ones((3, 3, 3), dtype=np.uint8)
        for _ in range(max(prune_iters, 0)):
            if skel.sum() == 0:
                break
            neighbours = _ndi.convolve(skel.astype(np.uint8), kernel, mode="constant") - skel.astype(np.uint8)
            endpoints = skel & (neighbours <= 1)
            if not endpoints.any():
                break
            skel = skel & ~endpoints

        coords = np.argwhere(skel)
        if coords.shape[0] < min_vox:
            return None

        diameters = 2.0 * dt[coords[:, 0], coords[:, 1], coords[:, 2]]
        diameters = diameters[diameters > 0]
        if diameters.size < min_vox:
            return None

        d_ref = float(np.percentile(diameters, ref_pct))
        if d_ref < min_ref_mm:
            return None  # too small to be a gradeable coronary segment
        d_min = float(np.percentile(diameters, min_pct))
        stenosis_pct = max(0.0, min(100.0, (d_ref - d_min) / d_ref * 100.0))

        length_mm = float(coords.shape[0]) * float(np.mean(voxel_spacing_mm))
        return {
            "stenosis_pct": round(stenosis_pct, 1),
            "grade": self._grade_stenosis(stenosis_pct),
            "reference_diameter_mm": round(d_ref, 2),
            "min_lumen_diameter_mm": round(d_min, 2),
            "centerline_length_mm": round(length_mm, 1),
        }

    def _run_lumen_segmentation(
        self, ccta_hu: np.ndarray, voxel_spacing_mm: tuple[float, float, float]
    ) -> dict[str, Any]:
        """Learned coronary lumen segmentation → centerline → per-vessel stenosis.

        Active ONLY when a lumen-segmentation model is loaded
        (``model.custom_ccta_weights_path``). Steps: (1) SwinUNETR lumen
        segmentation on the contrast CCTA, (2) split the lumen into connected
        vessel trees, (3) per vessel skeletonise + distance-transform to obtain a
        diameter profile and a diameter-stenosis estimate (see _vessel_stenosis).

        IMPORTANT LIMITATION: vessels are reported as geometric branches, NOT as
        SCCT 18-segment anatomical labels — true segment assignment requires a
        labelled coronary model / centerline registration, which is not done
        here. Callers surface this via the ``stenosis_segment_labels_approximate``
        QA flag. Returns ``segments`` (sorted worst-first) and ``max_stenosis_pct``.
        """
        if self._model is None:
            raise NotImplementedError("no coronary lumen-segmentation model loaded")
        lumen_mask = self._run_lumen_inference(ccta_hu)
        return self._grade_lumen_mask(lumen_mask, voxel_spacing_mm)

    def _grade_lumen_mask(
        self, lumen_mask: np.ndarray, voxel_spacing_mm: tuple[float, float, float]
    ) -> dict[str, Any]:
        """Geometric per-vessel stenosis grading on a binary coronary lumen mask.

        Source-agnostic: the mask may come from the learned SwinUNETR model
        (_run_lumen_inference) or from TotalSegmentator's coronary_arteries task
        (_run_coronary_lumen_totalseg). Splits the lumen into connected vessel
        trees and, per vessel, skeletonises + distance-transforms to estimate a
        diameter-stenosis percentage (see _vessel_stenosis). Vessels are reported
        as geometric branches, NOT SCCT 18-segment anatomical labels. Returns
        ``segments`` (sorted worst-first) and ``max_stenosis_pct``.
        """
        from scipy import ndimage as _ndi

        if lumen_mask is None or lumen_mask.sum() == 0:
            logger.info("lumen_segmentation_empty")
            return {"segments": [], "max_stenosis_pct": 0.0}

        scfg = self._cfg.get("stenosis", {})
        # Voxel volume for the per-vessel size gate.
        voxel_vol_mm3 = float(np.prod(voxel_spacing_mm))
        min_vessel_mm3 = float(scfg.get("min_vessel_volume_mm3", 50.0))

        labeled, n = _ndi.label(lumen_mask)
        segments: list[dict[str, Any]] = []
        for cid in range(1, n + 1):
            comp = labeled == cid
            if float(comp.sum()) * voxel_vol_mm3 < min_vessel_mm3:
                continue
            graded = self._vessel_stenosis(comp, voxel_spacing_mm, scfg)
            if graded is None:
                continue
            graded["name"] = f"Vessel {len(segments) + 1} (geometric)"
            graded["vessel"] = "unassigned"
            segments.append(graded)

        segments.sort(key=lambda s: s["stenosis_pct"], reverse=True)
        max_stenosis_pct = segments[0]["stenosis_pct"] if segments else 0.0

        logger.info(
            "lumen_grading_complete",
            vessels_graded=len(segments),
            max_stenosis_pct=max_stenosis_pct,
        )
        return {"segments": segments, "max_stenosis_pct": max_stenosis_pct}

    def _run_coronary_lumen_totalseg(
        self,
        ccta_nifti_path: str,
        hu_shape: tuple[int, int, int],
        voxel_spacing_mm: tuple[float, float, float],
        working_dir: str,
    ) -> np.ndarray | None:
        """Coronary lumen mask from TotalSegmentator's ``coronary_arteries`` task.

        Needs NO external weights — it uses the TotalSegmentator model cache
        already relied on by the heart-ROI step and the other CT pipelines. Runs
        the task on the contrast CCTA, unions whatever coronary label file(s) it
        emits, and returns a binary uint8 mask aligned to the CCTA grid (or None on
        any failure, so infer() falls back to calcium-only). The mask is fed to the
        shared geometric grader (_grade_lumen_mask).

        NOTE: some TotalSegmentator versions gate the coronary_arteries task behind
        a free non-commercial license — set ``coronary_lumen.totalseg_license_number``
        to apply it; without it the task may fail and the pipeline degrades to
        calcium-only.
        """
        import glob

        from totalsegmentator.python_api import totalsegmentator as ts_run
        import torch

        lumen_cfg = self._cfg.get("coronary_lumen", {})
        task = lumen_cfg.get("totalseg_task", "coronary_arteries")
        license_number = lumen_cfg.get("totalseg_license_number")
        if license_number:
            try:
                from totalsegmentator.config import set_license_number
                set_license_number(str(license_number))
            except Exception as exc:
                logger.warning("totalseg_set_license_failed", error=str(exc))

        device = "gpu" if torch.cuda.is_available() else "cpu"
        ts_out = os.path.join(working_dir, "ccta_coronary_seg")
        os.makedirs(ts_out, exist_ok=True)

        logger.info("coronary_lumen_totalseg_start", task=task, device=device)
        ts_run(
            input=Path(ccta_nifti_path),
            output=Path(ts_out),
            task=task,
            device=device,
            quiet=True,
        )

        # The task may emit a single coronary_arteries.nii.gz or several per-branch
        # label files; union whatever .nii.gz masks were produced.
        mask = np.zeros(tuple(hu_shape), dtype=bool)
        primary = os.path.join(ts_out, "coronary_arteries.nii.gz")
        candidates = (
            [primary] if os.path.exists(primary)
            else sorted(glob.glob(os.path.join(ts_out, "*.nii.gz")))
        )
        found = False
        for path in candidates:
            m = nib.load(path).get_fdata() > 0.5
            if m.shape != tuple(hu_shape):
                logger.warning(
                    "coronary_lumen_shape_mismatch",
                    path=path, mask_shape=tuple(m.shape), hu_shape=tuple(hu_shape),
                )
                continue
            mask |= m
            found = True
        if not found or not mask.any():
            logger.warning("coronary_lumen_empty_or_missing", out_dir=ts_out)
            return None
        logger.info("coronary_lumen_totalseg_complete", lumen_voxels=int(mask.sum()))
        return mask.astype(np.uint8)

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

        # ── Lumen stenosis analysis (optional) ────────────────────────────────
        # Two lumen sources feed the SAME geometric per-vessel grader, in priority
        # order: (1) the learned SwinUNETR model when weights are loaded, then
        # (2) TotalSegmentator's coronary_arteries task (no external weights) when
        # coronary_lumen.use_totalseg_coronary is enabled. Either is optional; any
        # failure falls back to calcium-only.
        segments: list[dict[str, Any]] = []
        max_stenosis_pct: float | None = None
        inference_method = "calcium_only"

        lumen_cfg = self._cfg.get("coronary_lumen", {})
        ccta_path = preprocessed.get("ccta_nifti_path")
        _stenosis_note = (
            "Per-vessel stenosis is a geometric diameter estimate "
            "(skeleton + distance transform) on the coronary lumen mask; "
            "vessels are NOT mapped to SCCT 18-segment anatomy."
        )

        if self._model is not None and ccta_path:
            try:
                ccta_img = nib.load(ccta_path)
                ccta_hu = ccta_img.get_fdata().astype(np.float32)
                ccta_affine = ccta_img.affine
                ccta_spacing = tuple(abs(float(ccta_affine[i, i])) for i in range(3))
                dl_out = self._run_lumen_segmentation(ccta_hu, ccta_spacing)
                segments = dl_out.get("segments", [])
                max_stenosis_pct = dl_out.get("max_stenosis_pct")
                inference_method = "dl_stenosis"
                qa_flags.append("stenosis_segment_labels_approximate")
                qa_details["stenosis_note"] = _stenosis_note
            except NotImplementedError:
                logger.info("lumen_segmentation_not_implemented_using_calcium_only")
            except Exception as exc:
                logger.warning("dl_stenosis_failed_using_calcium_only", error=str(exc))
        elif lumen_cfg.get("use_totalseg_coronary", False) and ccta_path:
            try:
                ccta_img = nib.load(ccta_path)
                ccta_affine = ccta_img.affine
                ccta_spacing = tuple(abs(float(ccta_affine[i, i])) for i in range(3))
                lumen_mask = self._run_coronary_lumen_totalseg(
                    ccta_path, ccta_img.shape, ccta_spacing, working_dir
                )
                if lumen_mask is not None:
                    graded = self._grade_lumen_mask(lumen_mask, ccta_spacing)
                    segments = graded.get("segments", [])
                    max_stenosis_pct = graded.get("max_stenosis_pct")
                    inference_method = "totalseg_coronary_stenosis"
                    qa_flags.append("stenosis_segment_labels_approximate")
                    qa_details["stenosis_note"] = _stenosis_note
                    qa_details["lumen_source"] = "totalsegmentator_coronary_arteries"
                else:
                    qa_flags.append("coronary_lumen_unavailable")
            except Exception as exc:
                logger.warning("totalseg_coronary_failed_using_calcium_only", error=str(exc))
                qa_flags.append("coronary_lumen_unavailable")

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
        stenosis_available = inference_method in ("dl_stenosis", "totalseg_coronary_stenosis")

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

        # Segmentation NIfTI. Stored under the conventional `segmentation.nii.gz`
        # name (not a coronary-specific name) so the shared consumers find it:
        # the on-demand preview-overlay endpoint and the Phase-7 DICOM Seg export
        # both look up segmentation masks by that name.
        if calcium_mask is not None and affine is not None:
            seg_path = os.path.join(artifacts_dir, "segmentation.nii.gz")
            nib.save(nib.Nifti1Image(calcium_mask, affine), seg_path)
            artifacts.append({
                "name": "segmentation.nii.gz",
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
            "totalseg_coronary_stenosis": (
                "TotalSegmentator coronary-artery lumen + geometric stenosis grading "
                "+ Agatston calcium scoring"
            ),
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
