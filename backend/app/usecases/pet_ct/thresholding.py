"""Stage 1 — Component labeling & segmentation.

Everything that turns a calibrated SUV volume + co-registered CT into a list of
discrete, measured, anatomically-named lesion clusters:

* **Reference SUV statistics** — liver / mediastinal-blood-pool means used both
  as the detection threshold basis and for Deauville scoring.
* **Detection threshold** — the (config-driven, unchanged) PERCIST / liver-mean
  logic. The spec's ``SUV > 1.5 × Liver_Mean`` is the default *calibrated*
  behaviour via ``calibrated_liver_factor: 1.5`` in ``inference_config.yaml``.
* **TotalSegmentator organ masks** — a single multilabel pass on the native CT
  serves reference-organ stats, physiologic suppression, and lesion naming.
* **Connected-component clustering** — ``scipy.ndimage.label`` isolates distinct
  spatial voxel clusters (individual lesions).
* **Cross-referencing** — each cluster is majority-voted against the
  TotalSegmentator map to attach an anatomical "ground truth" label, and its
  size / SUV / CT density are measured.

All heavy numerical helpers are pure functions; the TotalSegmentator runners are
process-isolated subprocess calls (daemonic-Celery-safe). Orchestration lives in
``pipeline.py``; this module holds the computation.
"""
from __future__ import annotations

import os
from typing import Any

import nibabel as nib
import numpy as np
import structlog
from scipy import ndimage

from app.usecases.pet_ct import registration

logger = structlog.get_logger(__name__)

# TotalSegmentator structures whose FDG uptake is normally physiologic/excretory.
# A focus localising here is flagged likely-physiologic rather than a lesion.
PHYSIOLOGIC_STRUCTURES = {
    "brain", "heart", "myocardium", "urinary_bladder",
    "kidney_left", "kidney_right", "kidney_cyst_left", "kidney_cyst_right",
}

# Bony structures — used to veto implausible bone naming against a focus's own CT
# density. Vertebrae/ribs use per-level names, matched by prefix.
BONE_STRUCTURE_NAMES = {
    "skull", "sternum", "sacrum", "hip_left", "hip_right",
    "clavicula_left", "clavicula_right", "scapula_left", "scapula_right",
    "humerus_left", "humerus_right", "femur_left", "femur_right",
    "patella_left", "patella_right", "tibia_left", "tibia_right",
    "fibula_left", "fibula_right",
}
BONE_STRUCTURE_PREFIXES = ("vertebrae_", "rib_", "costal_")


def is_bone_structure(name: str | None) -> bool:
    if not name:
        return False
    return name in BONE_STRUCTURE_NAMES or name.startswith(BONE_STRUCTURE_PREFIXES)


# ── Reference SUV statistics ────────────────────────────────────────────────────

def extract_reference_region_stats(
    pet_arr: np.ndarray, ct_arr: np.ndarray, cfg: dict
) -> dict[str, dict[str, float]]:
    """Liver + mediastinum SUV stats from co-registered CT HU masks (HU-box heuristic)."""
    liver_hu_min = cfg.get("liver_hu_min", 40)
    liver_hu_max = cfg.get("liver_hu_max", 80)
    med_hu_min = cfg.get("mediastinum_hu_min", 20)
    med_hu_max = cfg.get("mediastinum_hu_max", 55)

    x, y, z = pet_arr.shape

    z_lo, z_hi = int(z * 0.15), int(z * 0.55)
    pet_liver_region = pet_arr[: x // 2, :, z_lo:z_hi]
    ct_liver_region = ct_arr[: x // 2, :, z_lo:z_hi]
    liver_mask = (ct_liver_region >= liver_hu_min) & (ct_liver_region <= liver_hu_max)
    liver_vals = pet_liver_region[liver_mask]

    xq, yq = x // 4, y // 4
    z_med_lo, z_med_hi = int(z * 0.40), int(z * 0.75)
    pet_med = pet_arr[xq : 3 * xq, yq : 3 * yq, z_med_lo:z_med_hi]
    ct_med = ct_arr[xq : 3 * xq, yq : 3 * yq, z_med_lo:z_med_hi]
    med_mask = (ct_med >= med_hu_min) & (ct_med <= med_hu_max)
    med_vals = pet_med[med_mask]

    stats: dict[str, dict[str, float]] = {}

    if len(liver_vals) >= 200:
        stats["liver"] = {
            "mean": float(np.mean(liver_vals)),
            "std": float(np.std(liver_vals)),
            "n_voxels": len(liver_vals),
        }
    else:
        valid = pet_arr[pet_arr > 0.5]
        if len(valid) > 0:
            stats["liver"] = {
                "mean": float(np.percentile(valid, 65)),
                "std": float(np.std(valid) * 0.25),
                "n_voxels": 0,
                "fallback": True,
            }
        else:
            stats["liver"] = {"mean": 2.0, "std": 0.5, "n_voxels": 0, "fallback": True}
        logger.warning("liver_roi_fallback", liver_voxels_found=len(liver_vals))

    if len(med_vals) >= 100:
        stats["mediastinum"] = {
            "mean": float(np.mean(med_vals)),
            "std": float(np.std(med_vals)),
            "n_voxels": len(med_vals),
        }
    else:
        stats["mediastinum"] = {
            "mean": stats["liver"]["mean"] * 0.5,
            "std": 0.2,
            "n_voxels": 0,
            "fallback": True,
        }
        logger.warning("mediastinum_roi_fallback", med_voxels_found=len(med_vals))

    return stats


def suv_stats_from_mask(
    suv_arr: np.ndarray, mask: np.ndarray | None, erode_iter: int = 1, min_voxels: int = 50
) -> dict[str, float] | None:
    """Mean/SD SUV inside an organ segmentation mask (eroded to drop partial-volume edge)."""
    if mask is None or not mask.any():
        return None
    use = mask
    if erode_iter > 0:
        eroded = ndimage.binary_erosion(mask, iterations=erode_iter)
        if int(eroded.sum()) >= min_voxels:
            use = eroded
    vals = suv_arr[use]
    vals = vals[vals > 0]
    if len(vals) < min_voxels:
        return None
    return {
        "mean": float(np.mean(vals)),
        "std": float(np.std(vals)),
        "n_voxels": int(len(vals)),
    }


# ── CT-density & size measurement ───────────────────────────────────────────────

def ct_hu_on_original(
    orig_ct_img, mask: np.ndarray, suv_affine: np.ndarray, erode_iter: int = 1
) -> float | None:
    """Median CT HU on the ORIGINAL-resolution CT over a PET-space lesion mask."""
    core = mask
    if erode_iter > 0:
        eroded = ndimage.binary_erosion(mask, iterations=erode_iter)
        if int(eroded.sum()) >= 5:
            core = eroded
    ijk = np.argwhere(core)
    if ijk.size == 0:
        return None
    world = nib.affines.apply_affine(suv_affine, ijk)
    ct_ijk = np.rint(
        nib.affines.apply_affine(np.linalg.inv(orig_ct_img.affine), world)
    ).astype(int)
    ct_data = orig_ct_img.get_fdata()
    shp = np.array(ct_data.shape)
    inside = np.all((ct_ijk >= 0) & (ct_ijk < shp), axis=1)
    ct_ijk = ct_ijk[inside]
    if len(ct_ijk) == 0:
        return None
    vals = ct_data[ct_ijk[:, 0], ct_ijk[:, 1], ct_ijk[:, 2]]
    return round(float(np.median(vals)), 1)


def dims_from_mask(mask: np.ndarray, spacing) -> dict[str, Any] | None:
    """Bounding-box dimensions (cm, descending) of a boolean mask given voxel spacing (mm)."""
    ijk = np.argwhere(mask)
    if ijk.size == 0:
        return None
    extent_vox = ijk.max(0) - ijk.min(0) + 1
    dims_mm = np.sort(np.asarray(extent_vox, dtype=float) * np.asarray(spacing, dtype=float))[::-1]
    dims_cm = [round(float(d) / 10.0, 1) for d in dims_mm]
    return {
        "dimensions_cm": dims_cm,
        "long_diameter_cm": dims_cm[0],
        "short_diameter_cm": dims_cm[1] if len(dims_cm) > 1 else dims_cm[0],
    }


def measure_lesion_ct(
    orig_ct_img, comp_mask: np.ndarray, suv_affine: np.ndarray,
    metabolic_vol_ml: float, cfg: dict,
) -> dict[str, Any] | None:
    """PET-seeded, CT-constrained anatomic size of a lesion on the original CT.

    Region-grows a homogeneous soft-tissue component around the metabolic focus,
    bounded to a margin. Returns None (→ caller falls back to metabolic extent)
    when the CT boundary is untrustworthy (non-soft-tissue seed, leak, ROI-border
    contact). NOT a validated tumour segmenter — an estimate.
    """
    try:
        ct_data = orig_ct_img.get_fdata()
        ct_affine = orig_ct_img.affine
        ct_spacing = np.sqrt((ct_affine[:3, :3] ** 2).sum(axis=0))
        ct_vox_ml = float(np.prod(ct_spacing)) / 1000.0

        ijk = np.argwhere(comp_mask)
        if ijk.size == 0:
            return None
        world = nib.affines.apply_affine(suv_affine, ijk)
        ct_ijk = np.rint(nib.affines.apply_affine(np.linalg.inv(ct_affine), world)).astype(int)
        shp = np.array(ct_data.shape)
        inside = np.all((ct_ijk >= 0) & (ct_ijk < shp), axis=1)
        ct_ijk = ct_ijk[inside]
        if len(ct_ijk) < 3:
            return None

        focus_hu = ct_data[ct_ijk[:, 0], ct_ijk[:, 1], ct_ijk[:, 2]]
        seed_hu = float(np.median(focus_hu))
        if seed_hu < float(cfg.get("min_seed_hu", -50.0)) or seed_hu > float(cfg.get("max_seed_hu", 150.0)):
            return None

        margin_vox = np.ceil(float(cfg.get("roi_margin_mm", 20.0)) / np.maximum(ct_spacing, 0.1)).astype(int)
        lo = np.maximum(ct_ijk.min(0) - margin_vox, 0)
        hi = np.minimum(ct_ijk.max(0) + margin_vox + 1, shp)
        sub = ct_data[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
        if sub.size == 0:
            return None

        tol = float(cfg.get("hu_tolerance", 40.0))
        band = (sub >= seed_hu - tol) & (sub <= seed_hu + tol)
        if not band.any():
            return None
        labeled, n = ndimage.label(band)
        if n == 0:
            return None
        seed = np.clip(np.rint(ct_ijk.mean(0)).astype(int) - lo, 0, np.array(sub.shape) - 1)
        seed_label = int(labeled[seed[0], seed[1], seed[2]])
        if seed_label == 0:
            fv = np.clip(ct_ijk[len(ct_ijk) // 2] - lo, 0, np.array(sub.shape) - 1)
            seed_label = int(labeled[fv[0], fv[1], fv[2]])
            if seed_label == 0:
                return None
        seg = labeled == seed_label

        seg_vox = int(seg.sum())
        if seg_vox < int(cfg.get("min_seg_voxels", 8)):
            return None
        seg_vol_ml = seg_vox * ct_vox_ml

        if seg_vox / max(sub.size, 1) > float(cfg.get("max_roi_fill_frac", 0.6)):
            return None
        for ax in range(3):
            proj = seg.any(axis=tuple(a for a in range(3) if a != ax))
            if proj[0] and proj[-1]:
                return None
        if metabolic_vol_ml > 0 and seg_vol_ml > float(cfg.get("max_leak_factor", 20.0)) * metabolic_vol_ml:
            return None

        dims = dims_from_mask(seg, ct_spacing)
        if dims is None:
            return None
        dims["volume_ml"] = round(seg_vol_ml, 2)
        dims["source"] = "ct"
        return dims
    except Exception:
        return None


def compute_suv_peak(
    suv_arr: np.ndarray, lesion_mask: np.ndarray, voxel_vol_ml: float,
    sphere_radius_mm: float, voxel_spacing_mm: tuple[float, float, float],
) -> float:
    """SUVpeak = mean SUV within the hottest 1 cm³ sphere centred on the peak voxel."""
    sphere_radii_vox = tuple(sphere_radius_mm / max(s, 0.1) for s in voxel_spacing_mm)

    masked_suv = suv_arr * lesion_mask.astype(np.float32)
    flat_idx = np.argmax(masked_suv)
    peak_coord = np.unravel_index(flat_idx, suv_arr.shape)

    rz, ry, rx = (max(int(r) + 1, 1) for r in sphere_radii_vox)
    zz, yy, xx = np.ogrid[-rz : rz + 1, -ry : ry + 1, -rx : rx + 1]
    kernel = (
        (zz / max(sphere_radii_vox[2], 0.1)) ** 2
        + (yy / max(sphere_radii_vox[1], 0.1)) ** 2
        + (xx / max(sphere_radii_vox[0], 0.1)) ** 2
    ) <= 1.0

    z0, y0, x0 = peak_coord
    sz, sy, sx = suv_arr.shape

    z1, z2 = max(0, z0 - rz), min(sz, z0 + rz + 1)
    y1, y2 = max(0, y0 - ry), min(sy, y0 + ry + 1)
    x1, x2 = max(0, x0 - rx), min(sx, x0 + rx + 1)

    kz1 = rz - (z0 - z1)
    ky1 = ry - (y0 - y1)
    kx1 = rx - (x0 - x1)

    region = suv_arr[z1:z2, y1:y2, x1:x2]
    k_region = kernel[
        kz1 : kz1 + (z2 - z1),
        ky1 : ky1 + (y2 - y1),
        kx1 : kx1 + (x2 - x1),
    ]

    sphere_vals = region[k_region]
    return float(np.mean(sphere_vals)) if len(sphere_vals) > 0 else float(suv_arr[peak_coord])


# ── Scoring / diagnosis helpers ─────────────────────────────────────────────────

def deauville_score(suv_max: float, med_mean: float, liver_mean: float) -> int:
    if suv_max <= 0:
        return 1
    elif suv_max <= med_mean:
        return 2
    elif suv_max <= liver_mean:
        return 3
    elif suv_max <= liver_mean * 2.0:
        return 4
    else:
        return 5


def derive_diagnosis(
    lesions: list[dict],
    deauville: int,
    suv_cutoff: float,
    cutoff_label: str = "liver SUVmean",
) -> str:
    """Tumor-positive/negative call keyed off ``suv_cutoff`` (default liver SUVmean)."""
    if not lesions:
        return (
            f"Tumor Negative — No FDG-avid lesions detected above {cutoff_label} "
            f"(SUV {suv_cutoff:.1f}). No evidence of metabolically active disease."
        )
    n = len(lesions)
    suv_max = max(x["suv_max"] for x in lesions)
    if suv_max > suv_cutoff:
        return (
            f"Tumor Positive — {n} FDG-avid lesion(s) detected with SUVmax {suv_max:.1f} "
            f"(above {cutoff_label} {suv_cutoff:.1f}). Deauville {deauville}. "
            "Findings consistent with metabolically active disease; clinical correlation recommended."
        )
    else:
        return (
            f"Tumor Negative — {n} focus/foci with SUVmax {suv_max:.1f} ≤ {cutoff_label} "
            f"{suv_cutoff:.1f}. Deauville {deauville}. "
            "Uptake below tumor-positive threshold; likely physiological."
        )


def build_physiological_exclusion_mask(shape: tuple, cfg: dict | None = None) -> np.ndarray:
    """Boolean mask (True = exclude) for physiological FDG regions (brain/thyroid/bladder).

    Fractions assume a head-to-toe volume (superior = high Z index, axis 2).
    """
    cfg = cfg or {}
    x, y, z = shape
    mask = np.zeros(shape, dtype=bool)

    brain_frac   = cfg.get("exclude_brain_top_frac",   0.12)
    thyroid_lo   = cfg.get("exclude_thyroid_z_lo",     0.78)
    thyroid_hi   = cfg.get("exclude_thyroid_z_hi",     0.90)
    bladder_frac = cfg.get("exclude_bladder_bot_frac", 0.08)
    xy_lo        = cfg.get("exclude_organ_xy_lo",      0.35)
    xy_hi        = cfg.get("exclude_organ_xy_hi",      0.65)

    brain_z = int(z * (1 - brain_frac))
    mask[:, :, brain_z:] = True

    tz_lo, tz_hi = int(z * thyroid_lo), int(z * thyroid_hi)
    tx_lo, tx_hi = int(x * xy_lo), int(x * xy_hi)
    ty_lo, ty_hi = int(y * xy_lo), int(y * xy_hi)
    mask[tx_lo:tx_hi, ty_lo:ty_hi, tz_lo:tz_hi] = True

    bladder_z = int(z * bladder_frac)
    bx_lo, bx_hi = int(x * xy_lo), int(x * xy_hi)
    by_lo, by_hi = int(y * xy_lo), int(y * xy_hi)
    mask[bx_lo:bx_hi, by_lo:by_hi, :bladder_z] = True

    return mask


def estimate_anatomical_region(centroid_voxel: list[float], shape: tuple) -> str:
    """Estimate anatomical region from lesion centroid Z position (head-to-toe)."""
    _, _, z = shape
    z_frac = centroid_voxel[2] / max(z, 1)
    if z_frac >= 0.88:
        return "Brain"
    elif z_frac >= 0.75:
        return "Head/Neck"
    elif z_frac >= 0.58:
        return "Thorax"
    elif z_frac >= 0.40:
        return "Upper Abdomen"
    elif z_frac >= 0.20:
        return "Lower Abdomen/Pelvis"
    else:
        return "Pelvis/Perineum"


# ── Detection threshold (config-driven — unchanged behaviour) ───────────────────

def compute_detection_threshold(
    cfg_inf: dict, liver_mean: float, liver_std: float, suv_calibrated: bool
) -> tuple[float, dict[str, Any]]:
    """Resolve the SUV detection threshold using the existing, validated logic.

    The spec's ``SUV > 1.5 × Liver_Mean`` is the default calibrated behaviour when
    ``calibrated_threshold_source == "liver_mean"`` and
    ``calibrated_liver_factor == 1.5`` (both the shipped defaults). We do NOT
    hard-code 1.5 here — it stays config-driven so the deployed clinical numbers
    are preserved and remain site-tunable.

    Returns ``(threshold, details)`` where ``details`` records the reference
    PERCIST threshold and which branch fired (for logging / QA transparency).
    """
    suv_thresh_abs = cfg_inf.get("suv_threshold_absolute", 2.5)
    percist_factor = cfg_inf.get("percist_liver_factor", 1.5)
    percist_threshold = percist_factor * (liver_mean + 2.0 * liver_std)

    details: dict[str, Any] = {
        "percist_threshold": round(percist_threshold, 3),
        "suv_calibrated": suv_calibrated,
    }

    if not suv_calibrated and cfg_inf.get("uncalibrated_use_relative_threshold", True):
        # Uncalibrated: pixel values are relative intensities, so the absolute SUV
        # cutoff is meaningless. Use the liver-relative PERCIST threshold.
        threshold = max(percist_threshold, 0.1)
        details["source"] = "percist_relative"
    else:
        thr_source = cfg_inf.get("calibrated_threshold_source", "liver_mean")
        if thr_source == "liver_mean" and liver_mean > 0:
            liver_factor = float(cfg_inf.get("calibrated_liver_factor", 1.0))
            threshold = max(liver_factor * liver_mean, 0.1)
            details["source"] = "liver_mean"
            details["liver_factor"] = liver_factor
        else:
            threshold = suv_thresh_abs
            details["source"] = "absolute"

    details["effective_threshold"] = round(threshold, 3)
    return threshold, details


# ── Connected-component clustering + cross-referencing (Stage 1 core) ───────────

def label_and_measure_lesions(
    *,
    detection_mask: np.ndarray,
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    orig_ct_img,
    affine: np.ndarray,
    voxel_spacing: tuple[float, float, float],
    voxel_vol_ml: float,
    structure_labels: np.ndarray | None,
    structure_names: dict[int, str],
    liver_mean: float,
    cfg_inf: dict,
) -> tuple[list[dict[str, Any]], np.ndarray, dict[str, int]]:
    """Cluster the thresholded mask into lesions and measure/name each one.

    Runs ``scipy.ndimage.label`` to isolate spatial clusters, drops sub-threshold
    and implausibly-large components, applies CT concordance, then for each
    surviving cluster computes SUV metrics, CT density, anatomic size, and — by
    majority vote against the TotalSegmentator map — an anatomical structure name.

    Returns ``(lesions, cleaned_labels, rejected_counts)``. ``cleaned_labels`` has
    rejected components zeroed so downstream (display geometry, mask artifact)
    sees only accepted lesions. Each lesion carries a private ``_label_id`` (its
    component id in ``cleaned_labels``) for the registration stage; the caller
    strips it before persisting.
    """
    labeled, n_components = ndimage.label(detection_mask)

    # ── CT concordance setup ────────────────────────────────────────────────
    conc_cfg = cfg_inf.get("ct_concordance", {})
    conc_enabled = (
        bool(conc_cfg.get("enabled", True))
        and ct_arr is not None
        and ct_arr.shape == suv_arr.shape
    )
    if conc_enabled:
        # Degenerate CT (failed co-registration → all air) cannot discriminate;
        # disabling avoids wrongly rejecting every focus including a true tumour.
        ct_soft_frac = float(np.mean(ct_arr > -150.0))
        if ct_soft_frac < 0.01:
            logger.warning(
                "ct_concordance_disabled_degenerate_ct",
                soft_tissue_fraction=round(ct_soft_frac, 4),
            )
            conc_enabled = False
    conc_min_hu = float(conc_cfg.get("min_mean_hu", -150.0))
    conc_air_hu = float(conc_cfg.get("air_hu", -200.0))
    conc_max_air = float(conc_cfg.get("max_air_fraction", 0.5))

    min_vol_ml = cfg_inf.get("min_lesion_volume_ml", 1.2)
    sphere_r_mm = cfg_inf.get("suv_peak_sphere_radius_mm", 6.204)
    min_organ_coverage = float(
        cfg_inf.get("structure_labeling", {}).get("min_organ_coverage", 0.5)
    )

    # Oversized-focus sanity bound (diffuse uptake the suppression stages missed).
    max_lesion_ml = cfg_inf.get("max_lesion_volume_ml", 2000.0)
    max_lesion_frac = cfg_inf.get("max_lesion_volume_fraction", 0.10)
    total_volume_ml = float(suv_arr.size) * voxel_vol_ml
    max_frac_ml = max_lesion_frac * total_volume_ml

    ct_meas_cfg = cfg_inf.get("ct_lesion_measurement", {})

    n_rejected_concordance = 0
    n_rejected_oversize = 0
    lesions: list[dict[str, Any]] = []

    for comp_id in range(1, n_components + 1):
        comp_mask = labeled == comp_id
        vol_ml = float(np.sum(comp_mask)) * voxel_vol_ml
        if vol_ml < min_vol_ml:
            labeled[comp_mask] = 0
            continue

        if vol_ml > max_lesion_ml or vol_ml > max_frac_ml:
            labeled[comp_mask] = 0
            n_rejected_oversize += 1
            logger.warning(
                "oversized_focus_rejected",
                volume_ml=round(vol_ml, 1),
                max_lesion_ml=max_lesion_ml,
                max_fraction_ml=round(max_frac_ml, 1),
            )
            continue

        if conc_enabled:
            comp_ct = ct_arr[comp_mask]
            if comp_ct.size > 0:
                mean_hu = float(np.mean(comp_ct))
                air_frac = float(np.mean(comp_ct < conc_air_hu))
                if mean_hu < conc_min_hu or air_frac > conc_max_air:
                    labeled[comp_mask] = 0
                    n_rejected_concordance += 1
                    continue

        comp_suv = suv_arr[comp_mask]
        suv_max = float(np.max(comp_suv))
        suv_mean = float(np.mean(comp_suv))
        suv_peak = compute_suv_peak(suv_arr, comp_mask, voxel_vol_ml, sphere_r_mm, voxel_spacing)
        tlg = suv_mean * vol_ml
        centroid = ndimage.center_of_mass(comp_mask)

        # Anatomical structure by majority vote over the component's voxels in the
        # TotalSegmentator multilabel map. Only name after an organ covering at
        # least min_organ_coverage of the focus (tumour is not itself segmented, so
        # a minority organ clip would mislabel).
        structure = None
        physiologic = False
        if structure_labels is not None:
            comp_labels = structure_labels[comp_mask]
            comp_struct_vals = comp_labels[comp_labels > 0]
            if comp_struct_vals.size > 0:
                vals, counts = np.unique(comp_struct_vals, return_counts=True)
                top_idx = int(np.argmax(counts))
                top_id = int(vals[top_idx])
                top_coverage = float(counts[top_idx]) / float(comp_mask.sum())
                if top_coverage >= min_organ_coverage:
                    structure = structure_names.get(top_id)
                    physiologic = structure in PHYSIOLOGIC_STRUCTURES

        # CT density over the focus core (original diagnostic CT preferred).
        ct_mean_hu = None
        if orig_ct_img is not None:
            ct_mean_hu = ct_hu_on_original(orig_ct_img, comp_mask, affine)
        if ct_mean_hu is None and ct_arr is not None and ct_arr.shape == suv_arr.shape:
            comp_ct = ct_arr[comp_mask]
            if comp_ct.size > 0:
                ct_mean_hu = round(float(np.mean(comp_ct)), 1)

        # Plausibility veto: don't call a soft-tissue-density focus a bone lesion.
        if structure and is_bone_structure(structure) and ct_mean_hu is not None and ct_mean_hu < 150.0:
            structure = None
            physiologic = False

        # Lesion size: prefer CT-based anatomic measurement, fall back to metabolic.
        size = None
        if orig_ct_img is not None and ct_meas_cfg.get("enabled", True):
            size = measure_lesion_ct(orig_ct_img, comp_mask, affine, vol_ml, ct_meas_cfg)
        if size is None:
            size = dims_from_mask(comp_mask, voxel_spacing)
            if size is not None:
                size["source"] = "metabolic"

        lesions.append({
            "id": len(lesions) + 1,
            "_label_id": comp_id,
            "suv_max": round(suv_max, 2),
            "suv_mean": round(suv_mean, 2),
            "suv_peak": round(suv_peak, 2),
            "tlr": round(suv_max / liver_mean, 2) if liver_mean > 0 else None,
            "volume_ml": round(vol_ml, 2),
            "ct_mean_hu": ct_mean_hu,
            "tlg": round(tlg, 2),
            "anatomical_region": estimate_anatomical_region(
                [round(c, 1) for c in centroid], suv_arr.shape
            ),
            "structure": structure,
            "physiologic_uptake": physiologic,
            "dimensions_cm": size["dimensions_cm"] if size else None,
            "long_diameter_cm": size["long_diameter_cm"] if size else None,
            "short_diameter_cm": size["short_diameter_cm"] if size else None,
            "ct_volume_ml": size.get("volume_ml") if size else None,
            "size_source": size["source"] if size else None,
            "centroid_voxel": [round(c, 1) for c in centroid],
        })

    lesions.sort(key=lambda x: x["suv_max"], reverse=True)
    counts = {"oversize": n_rejected_oversize, "concordance": n_rejected_concordance}
    return lesions, labeled, counts


# ── TotalSegmentator runners (process-isolated; daemonic-Celery-safe) ───────────

def run_totalseg_ml(
    ct_nifti_path: str, task: str, suv_shape: tuple, working_dir: str, fast: bool,
    suv_reference_path: str,
) -> tuple[np.ndarray, dict[int, str]] | None:
    """One TotalSegmentator task in ``--ml`` (multilabel) mode via subprocess.

    ``ct_nifti_path`` MUST be the native-resolution diagnostic CT (the models do
    their own internal resampling; feeding a PET-grid-downsampled CT loses detail
    before the model sees it). Output label map is resampled onto the SUV grid via
    nearest-neighbor. CLI subprocess avoids the daemonic-Celery multiprocessing ban.
    """
    import subprocess

    try:
        import torch

        device = "gpu" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"

    out_dir = os.path.join(working_dir, f"petct_seg_{task}")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "seg.nii.gz")
    cmd = [
        "TotalSegmentator",
        "-i", str(ct_nifti_path),
        "-o", out_file,
        "-ta", task,
        "--ml",
        "-d", device,
    ]
    if fast and task == "total":
        cmd.append("--fast")  # 3 mm model — big runtime win, adequate for naming
    try:
        logger.info("structure_seg_start", task=task, device=device)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as exc:
        logger.warning("structure_seg_failed", task=task, error=str(exc))
        return None
    if proc.returncode != 0:
        logger.warning(
            "structure_seg_failed", task=task,
            returncode=proc.returncode, stderr=(proc.stderr or "")[-600:],
        )
        return None

    path = out_file
    if not os.path.exists(path):
        cand = [f for f in os.listdir(out_dir) if f.endswith((".nii", ".nii.gz"))]
        if not cand:
            logger.warning("structure_seg_no_output", task=task, out_dir=out_dir)
            return None
        path = os.path.join(out_dir, cand[0])

    resampled_path = os.path.join(out_dir, "seg_on_suv_grid.nii.gz")
    try:
        registration.resample_labelmap_to_reference(path, suv_reference_path, resampled_path)
    except Exception as exc:
        logger.warning("structure_seg_resample_failed", task=task, error=str(exc))
        return None

    arr = nib.load(resampled_path).get_fdata()
    if arr.shape != tuple(suv_shape):
        logger.warning(
            "structure_seg_shape_mismatch", task=task,
            got=tuple(arr.shape), expected=tuple(suv_shape),
        )
        return None
    labels = np.rint(arr).astype(np.int32)

    try:
        from totalsegmentator.map_to_binary import class_map
        id2name = {int(k): str(v) for k, v in dict(class_map.get(task, {})).items()}
    except Exception as exc:
        logger.warning("structure_classmap_unavailable", task=task, error=str(exc))
        return None
    if not id2name:
        logger.warning("structure_classmap_empty", task=task)
        return None
    return labels, id2name


def segment_structures_multilabel(
    ct_nifti_path: str, suv_shape: tuple, working_dir: str, suv_reference_path: str,
    struct_cfg: dict,
) -> tuple[np.ndarray, dict[int, str]] | None:
    """TotalSegmentator multilabel map for naming lesions by structure.

    Runs the free ``total`` task and merges configured extra free tasks (e.g.
    breasts) only where ``total`` is background, so extras never overwrite a named
    organ. Returns ``(labels, {id: name})`` or None (→ caller falls back to Z-region).
    """
    fast = struct_cfg.get("fast", True)

    total = run_totalseg_ml(ct_nifti_path, "total", suv_shape, working_dir, fast, suv_reference_path)
    if total is None:
        return None
    labels, id2name = total

    _LABEL_OFFSET = 10000
    for i, task in enumerate(struct_cfg.get("extra_tasks", ["breasts"]) or []):
        extra = run_totalseg_ml(ct_nifti_path, task, suv_shape, working_dir, fast, suv_reference_path)
        if extra is None:
            continue
        e_labels, e_names = extra
        off = _LABEL_OFFSET * (i + 1)
        merged = []
        for eid, ename in e_names.items():
            m = (e_labels == eid) & (labels == 0)
            if m.any():
                labels[m] = off + int(eid)
                id2name[off + int(eid)] = ename
                merged.append(ename)
        if merged:
            logger.info("structure_seg_merged", task=task, structures=merged)

    logger.info("structure_seg_complete", n_structures=int((np.unique(labels) > 0).sum()))
    return labels, id2name


def segment_reference_organs(
    ct_nifti_path: str, suv_shape: tuple, working_dir: str, suv_reference_path: str,
) -> dict[str, np.ndarray]:
    """Segment SUV reference organs (liver + aortic blood pool) via TotalSegmentator.

    Returns ``{organ: bool mask}`` (on the SUV grid) for organs segmented, ``{}``
    on any failure (→ caller falls back to the HU-box heuristic). Reference stats
    only — NOT added to the lesion-exclusion mask.
    """
    import subprocess

    try:
        import torch

        device = "gpu" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"

    organs = ["liver", "aorta"]
    ts_out = os.path.join(working_dir, "petct_reference_seg")
    os.makedirs(ts_out, exist_ok=True)

    cmd = [
        "TotalSegmentator",
        "-i", str(ct_nifti_path),
        "-o", ts_out,
        "-ta", "total",
        "-rs", *organs,
        "-d", device,
    ]
    try:
        logger.info("reference_totalseg_start", device=device, organs=organs)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            logger.warning(
                "reference_organ_seg_failed",
                returncode=proc.returncode,
                stderr=(proc.stderr or "")[-600:],
            )
            return {}
        masks: dict[str, np.ndarray] = {}
        for organ in organs:
            organ_path = os.path.join(ts_out, f"{organ}.nii.gz")
            if not os.path.exists(organ_path):
                continue
            resampled_path = os.path.join(ts_out, f"{organ}_on_suv_grid.nii.gz")
            try:
                registration.resample_labelmap_to_reference(organ_path, suv_reference_path, resampled_path)
            except Exception as exc:
                logger.warning("reference_organ_resample_failed", organ=organ, error=str(exc))
                continue
            mask = nib.load(resampled_path).get_fdata() > 0.5
            if mask.shape == tuple(suv_shape):
                masks[organ] = mask
        logger.info(
            "reference_totalseg_complete",
            segmented=list(masks.keys()),
            liver_voxels=int(masks["liver"].sum()) if "liver" in masks else 0,
        )
        return masks
    except Exception as exc:
        logger.warning("reference_organ_seg_failed", error=str(exc))
        return {}


def run_physiologic_organ_exclusion(
    ct_nifti_path: str, suv_shape: tuple, working_dir: str, supp_cfg: dict[str, Any],
    suv_reference_path: str,
) -> tuple[np.ndarray, list[str]] | None:
    """Anatomy-aware physiologic FDG exclusion mask via TotalSegmentator.

    ORs the configured organ masks into a boolean exclusion mask on the SUV grid.
    Returns ``(mask, organs)`` or None (→ caller falls back to the geometric mask).
    """
    import subprocess

    task = supp_cfg.get("totalseg_task", "total")
    organs = list(supp_cfg.get("exclude_organs", []) or [])
    dilate = int(supp_cfg.get("dilate_voxels", 0))
    if not organs:
        return None

    try:
        import torch

        device = "gpu" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"
    ts_out = os.path.join(working_dir, "petct_physio_seg")
    os.makedirs(ts_out, exist_ok=True)

    cmd = [
        "TotalSegmentator",
        "-i", str(ct_nifti_path),
        "-o", ts_out,
        "-ta", task,
        "-rs", *organs,
        "-d", device,
    ]
    logger.info("physiologic_totalseg_start", task=task, device=device, organs=organs)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as exc:
        logger.warning("physiologic_organ_seg_failed", error=str(exc))
        return None
    if proc.returncode != 0:
        logger.warning(
            "physiologic_organ_seg_failed",
            returncode=proc.returncode,
            stderr=(proc.stderr or "")[-600:],
        )
        return None

    excl = np.zeros(suv_shape, dtype=bool)
    found: list[str] = []
    for organ in organs:
        organ_path = os.path.join(ts_out, f"{organ}.nii.gz")
        if not os.path.exists(organ_path):
            logger.warning("physiologic_organ_missing", organ=organ)
            continue
        resampled_path = os.path.join(ts_out, f"{organ}_on_suv_grid.nii.gz")
        try:
            registration.resample_labelmap_to_reference(organ_path, suv_reference_path, resampled_path)
        except Exception as exc:
            logger.warning("physiologic_organ_resample_failed", organ=organ, error=str(exc))
            continue
        mask = nib.load(resampled_path).get_fdata() > 0.5
        if mask.shape != tuple(suv_shape):
            logger.warning(
                "physiologic_organ_shape_mismatch",
                organ=organ, organ_shape=mask.shape, suv_shape=tuple(suv_shape),
            )
            continue
        excl |= mask
        found.append(organ)

    if not found:
        return None
    if dilate > 0:
        excl = ndimage.binary_dilation(excl, iterations=dilate)
    logger.info(
        "physiologic_totalseg_complete",
        excluded_organs=found, excluded_voxels=int(excl.sum()),
    )
    return excl, found


def masks_from_labels(
    labels: np.ndarray, id2name: dict[int, str], names
) -> dict[str, np.ndarray]:
    """Per-structure boolean masks for the requested names from a multilabel map."""
    name2id: dict[str, int] = {}
    for i, nm in id2name.items():
        name2id.setdefault(nm, i)
    out: dict[str, np.ndarray] = {}
    for nm in names:
        i = name2id.get(nm)
        if i is None:
            continue
        m = labels == i
        if m.any():
            out[nm] = m
    return out
