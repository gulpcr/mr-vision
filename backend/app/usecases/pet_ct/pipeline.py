"""Whole-body FDG-PET/CT oncology pipeline.

Phases:
  preprocess  — download raw PET DICOMs, extract SUV calibration params,
                build SUV NIfTI, download/resample CT to PET grid
  infer       — PERCIST 1.0 threshold detection, connected-component lesion
                labelling, per-lesion SUV/MTV/TLG, Deauville reference stats
  postprocess — compile result dict, generate axial/coronal/sagittal MIP PNGs
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
import pydicom
import SimpleITK as sitk
import structlog
import yaml
from scipy import ndimage

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.usecases.base import BasePipeline

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

_F18_HALF_LIFE_SEC = 6586.2  # 109.77 min

_PET_PATTERNS = [
    r"(?i)\bpt\b", r"(?i)\bpet\b", r"(?i)emission",
    r"(?i)\bfdg\b", r"(?i)wholebody", r"(?i)whole.*body",
]
_CT_PATTERNS = [
    r"(?i)\bct\b", r"(?i)attenuation", r"(?i)transmission",
    r"(?i)low.*dose", r"(?i)ct.*corr",
]
# Non-diagnostic CT acquisitions (single-plane scouts) that must never be
# selected as the fusion/reference CT: a topogram is a 1-slice projection whose
# geometry does not overlap the PET volume, so resampling it onto the PET grid
# yields an all-air (-1000 HU) volume — a black CT background in the viewer and
# broken reference-region stats. See _classify_series.
_CT_SCOUT_PATTERNS = [
    r"(?i)topogram", r"(?i)scout", r"(?i)localizer", r"(?i)localiser",
    r"(?i)surview", r"(?i)scanogram", r"(?i)\bscano\b",
]


# ── DICOM / SUV helpers ────────────────────────────────────────────────────────

def _parse_dicom_time(time_str: str) -> float:
    """Parse DICOM HHMMSS.frac → seconds since midnight."""
    s = str(time_str or "").strip()
    if not s:
        return 0.0
    try:
        main, frac_sec = (s.split(".", 1) + ["0"])[:2]
        frac_sec = float("0." + frac_sec)
        main = main.zfill(6)
        return int(main[0:2]) * 3600 + int(main[2:4]) * 60 + int(main[4:6]) + frac_sec
    except (ValueError, IndexError):
        return 0.0


def _extract_suv_params(dicom_path: str) -> dict[str, Any]:
    """Read SUV calibration parameters from a PET DICOM file header."""
    ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)

    params: dict[str, Any] = {
        "units": "",
        "patient_weight_g": 0.0,
        "injected_dose_bq": 0.0,
        "half_life_sec": _F18_HALF_LIFE_SEC,
        "injection_time_sec": 0.0,
        "scan_time_sec": 0.0,
        "decay_correction": "ADMIN",
        "rescale_slope": 1.0,
        "rescale_intercept": 0.0,
        "radionuclide": "18F",
    }

    if hasattr(ds, "Units"):
        params["units"] = str(ds.Units)

    try:
        if hasattr(ds, "PatientWeight") and ds.PatientWeight:
            params["patient_weight_g"] = float(ds.PatientWeight) * 1000.0
    except (ValueError, TypeError):
        pass

    try:
        if hasattr(ds, "RescaleSlope"):
            params["rescale_slope"] = float(ds.RescaleSlope)
        if hasattr(ds, "RescaleIntercept"):
            params["rescale_intercept"] = float(ds.RescaleIntercept)
    except (ValueError, TypeError):
        pass

    if hasattr(ds, "DecayCorrection"):
        params["decay_correction"] = str(ds.DecayCorrection)

    rp_seq = getattr(ds, "RadiopharmaceuticalInformationSequence", None)
    if rp_seq and len(rp_seq) > 0:
        rp = rp_seq[0]
        try:
            if hasattr(rp, "RadionuclideTotalDose") and rp.RadionuclideTotalDose:
                params["injected_dose_bq"] = float(rp.RadionuclideTotalDose)
        except (ValueError, TypeError):
            pass
        try:
            if hasattr(rp, "RadionuclideHalfLife") and rp.RadionuclideHalfLife:
                params["half_life_sec"] = float(rp.RadionuclideHalfLife)
        except (ValueError, TypeError):
            pass
        if hasattr(rp, "RadiopharmaceuticalStartTime"):
            params["injection_time_sec"] = _parse_dicom_time(
                str(rp.RadiopharmaceuticalStartTime)
            )
        nuc_seq = getattr(rp, "RadionuclideCodeSequence", None)
        if nuc_seq and len(nuc_seq) > 0:
            params["radionuclide"] = str(getattr(nuc_seq[0], "CodeMeaning", "18F"))

        # Tracer name for display
        tracer_name = str(getattr(rp, "Radiopharmaceutical", "") or "")
        if tracer_name:
            params["tracer_name"] = tracer_name

    acq_time = getattr(ds, "AcquisitionTime", None) or getattr(ds, "SeriesTime", None)
    if acq_time:
        params["scan_time_sec"] = _parse_dicom_time(str(acq_time))

    return params


def _compute_suv_factor(params: dict[str, Any]) -> float:
    """Return the multiplier to convert Bq/mL pixel values to SUV."""
    weight_g = params.get("patient_weight_g", 0.0)
    injected_bq = params.get("injected_dose_bq", 0.0)
    if weight_g <= 0 or injected_bq <= 0:
        return 0.0

    half_life_sec = params.get("half_life_sec", _F18_HALF_LIFE_SEC)
    decay_correction = params.get("decay_correction", "ADMIN")
    scan_time = params.get("scan_time_sec", 0.0)
    injection_time = params.get("injection_time_sec", 0.0)

    if decay_correction in ("ADMIN", "NONE"):
        dose_at_scan = injected_bq
    else:
        delta = scan_time - injection_time
        if delta < 0:
            delta += 86400.0  # midnight rollover
        dose_at_scan = injected_bq * (0.5 ** (delta / max(half_life_sec, 1.0)))

    return weight_g / max(dose_at_scan, 1.0)


def _build_suv_nifti(
    dicom_dir: str, suv_params: dict, output_path: str, raw_output_path: str | None = None
) -> str:
    """Convert a PET DICOM series directory to a calibrated SUV NIfTI.

    Uses SimpleITK to read the DICOM series (preserves geometry), applies
    rescale slope/intercept to get Bq/mL, then multiplies by the SUV factor.

    When ``raw_output_path`` is given, the raw PET activity volume (Bq/mL, i.e.
    pixel × slope + intercept, *before* SUV normalisation) is also written there
    with identical geometry. The raw volume is used for display rendering so the
    viewer shows acquired PET intensities rather than the SUV-normalised values;
    SUV remains the basis for all quantitative measurements.
    """
    reader = sitk.ImageSeriesReader()
    series_ids = reader.GetGDCMSeriesIDs(dicom_dir)
    if series_ids:
        file_names = reader.GetGDCMSeriesFileNames(dicom_dir, series_ids[0])
    else:
        file_names = sorted(Path(dicom_dir).glob("*.dcm"))
        file_names = [str(p) for p in file_names]

    if not file_names:
        raise ValueError(f"No DICOM files found in {dicom_dir}")

    reader.SetFileNames(file_names)
    img = reader.Execute()

    arr = sitk.GetArrayFromImage(img).astype(np.float32)  # (Z, Y, X) or (Y, X) or (Z, Y, X, C)

    # Normalize to 3D (Z, Y, X)
    if arr.ndim == 2:
        # Single 2D slice
        arr = arr[np.newaxis, :, :]
    elif arr.ndim == 4:
        # Multi-component (e.g. RGB secondary capture) — collapse channels to luminance
        n_ch = arr.shape[-1]
        logger.warning("pet_dicom_multichannel", original_shape=list(arr.shape), channels=n_ch)
        if n_ch == 1:
            arr = arr[..., 0]
        elif n_ch == 3:
            # RGB → luminance (ITU-R BT.601)
            arr = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
        else:
            arr = arr.mean(axis=-1)
    elif arr.ndim != 3:
        raise ValueError(f"Unsupported PET array shape: {arr.shape}")

    # SimpleITK's ImageSeriesReader already applies each slice's Rescale
    # Slope/Intercept (the modality LUT) when reading the series, so `arr` is
    # ALREADY activity concentration in Bq/mL. Do NOT reapply slope/intercept here
    # — doing so double-scales the PET and makes every SUV value too low by the
    # slope factor (e.g. liver ~0.8 instead of ~2.7). Verified: SITK array ==
    # raw_pixel * RescaleSlope + RescaleIntercept.
    bqml = np.clip(arr, 0.0, None)

    suv_factor = _compute_suv_factor(suv_params)
    if suv_factor > 0:
        suv = bqml * suv_factor
    else:
        # Fallback: relative normalization when calibration data absent
        p99 = float(np.percentile(bqml[bqml > 0], 99)) if np.any(bqml > 0) else 1.0
        suv = bqml / (p99 / 8.0)
        logger.warning("suv_calibration_fallback", reason="missing_weight_or_dose")

    # Write the SUV NIfTI the SAME way the CT series is converted
    # (OrthancPACSClient._convert_dicom_to_nifti → sitk.WriteImage): build a
    # SimpleITK image from the SUV array and inherit the PET DICOM geometry, then
    # write with sitk. Previously the affine was assembled by hand from SITK's
    # (LPS) direction/origin and saved via nibabel, producing an LPS-signed NIfTI
    # inconsistent with the RAS NIfTI sitk writes for the CT — so the CT resampled
    # to PET space came out entirely -1000 (air): no fused CT background, no
    # organ-based physiologic suppression, and broken reference-region stats.
    suv_img = sitk.GetImageFromArray(suv)   # suv is (Z, Y, X) — SITK index order
    suv_img.SetSpacing(img.GetSpacing())
    suv_img.SetOrigin(img.GetOrigin())
    suv_img.SetDirection(img.GetDirection())
    sitk.WriteImage(suv_img, output_path)

    # Raw PET activity (Bq/mL), same geometry — used for display rendering only.
    if raw_output_path is not None:
        raw_img = sitk.GetImageFromArray(bqml)
        raw_img.SetSpacing(img.GetSpacing())
        raw_img.SetOrigin(img.GetOrigin())
        raw_img.SetDirection(img.GetDirection())
        sitk.WriteImage(raw_img, raw_output_path)

    logger.info(
        "suv_nifti_built",
        shape=list(suv.shape[::-1]),  # report as (X, Y, Z)
        suv_max=round(float(np.percentile(suv[suv > 0], 99.9)) if np.any(suv > 0) else 0.0, 2),
        suv_factor=round(suv_factor, 4),
    )
    return output_path


def _resample_ct_to_pet(pet_path: str, ct_path: str, output_path: str) -> str:
    """Resample CT to the PET voxel grid (PET/CT scanners are inherently co-registered)."""
    pet_img = sitk.ReadImage(pet_path, sitk.sitkFloat32)
    ct_img = sitk.ReadImage(ct_path, sitk.sitkFloat32)

    resampler = sitk.ResampleImageFilter()
    resampler.SetReferenceImage(pet_img)
    resampler.SetInterpolator(sitk.sitkLinear)
    resampler.SetDefaultPixelValue(-1000.0)
    ct_resampled = resampler.Execute(ct_img)
    sitk.WriteImage(ct_resampled, output_path)
    return output_path


def _extract_reference_region_stats(
    pet_arr: np.ndarray, ct_arr: np.ndarray, cfg: dict
) -> dict[str, dict[str, float]]:
    """Extract liver and mediastinum SUV stats from co-registered CT HU masks.

    Liver ROI: lower 60% of FOV, right half, HU 40–80
    Mediastinum ROI: middle thorax, central quarter, HU 20–55
    """
    liver_hu_min = cfg.get("liver_hu_min", 40)
    liver_hu_max = cfg.get("liver_hu_max", 80)
    med_hu_min = cfg.get("mediastinum_hu_min", 20)
    med_hu_max = cfg.get("mediastinum_hu_max", 55)

    x, y, z = pet_arr.shape

    # Liver: right half (x < x//2), lower 15–55% of FOV
    z_lo, z_hi = int(z * 0.15), int(z * 0.55)
    pet_liver_region = pet_arr[: x // 2, :, z_lo:z_hi]
    ct_liver_region = ct_arr[: x // 2, :, z_lo:z_hi]
    liver_mask = (ct_liver_region >= liver_hu_min) & (ct_liver_region <= liver_hu_max)
    liver_vals = pet_liver_region[liver_mask]

    # Mediastinum: central quarter XY, 40–75% of Z
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
        # Fallback: robust percentile approach when CT-based extraction fails
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


def _suv_stats_from_mask(
    suv_arr: np.ndarray, mask: np.ndarray | None, erode_iter: int = 1, min_voxels: int = 50
) -> dict[str, float] | None:
    """Mean/SD SUV inside an organ segmentation mask (e.g. TotalSegmentator liver).

    The mask is eroded a little first to drop boundary voxels (partial-volume with
    adjacent lung/heart/vessel), which would otherwise pull the reference mean down.
    Returns None when the mask is empty/missing or too small after erosion, so the
    caller can fall back to the heuristic ROI.
    """
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


def _ct_hu_on_original(
    orig_ct_img, mask: np.ndarray, suv_affine: np.ndarray, erode_iter: int = 1
) -> float | None:
    """Median CT HU on the ORIGINAL-resolution CT over a PET-space lesion mask.

    The lesion mask lives on the coarse PET/SUV grid; its voxels are mapped
    PET-grid -> world(mm) -> original-CT-grid through the two affines, then HU is
    read on the diagnostic CT. The mask is eroded to its core first to avoid
    air/edge partial-volume (which on the coarse CT drags soft-tissue HU toward
    air). Returns None when no voxel maps inside the CT volume.
    """
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


def _compute_suv_peak(
    suv_arr: np.ndarray, lesion_mask: np.ndarray, voxel_vol_ml: float,
    sphere_radius_mm: float, voxel_spacing_mm: tuple[float, float, float],
) -> float:
    """Compute SUVpeak as the mean SUV within the hottest 1 cm³ sphere."""
    sphere_radii_vox = tuple(sphere_radius_mm / max(s, 0.1) for s in voxel_spacing_mm)
    sphere_voxels = int(round((4 / 3) * np.pi * np.prod(sphere_radii_vox)))
    sphere_voxels = max(sphere_voxels, 1)

    masked_suv = suv_arr * lesion_mask.astype(np.float32)
    flat_idx = np.argmax(masked_suv)
    peak_coord = np.unravel_index(flat_idx, suv_arr.shape)

    # Build sphere kernel
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


def _deauville_score(suv_max: float, med_mean: float, liver_mean: float) -> int:
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


def _derive_diagnosis(
    lesions: list[dict],
    deauville: int,
    suv_cutoff: float,
    cutoff_label: str = "liver SUVmean",
) -> str:
    """Tumor-positive/negative call keyed off ``suv_cutoff``.

    ``suv_cutoff`` is the metabolic-significance threshold — by default the
    case's own liver SUVmean reference (physiologic background), so a focus is
    called positive when its SUVmax exceeds the patient's normal liver uptake
    rather than a fixed absolute SUV. ``cutoff_label`` names the cutoff in the
    diagnosis text (e.g. "liver SUVmean").
    """
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


def _build_physiological_exclusion_mask(shape: tuple, cfg: dict | None = None) -> np.ndarray:
    """
    Returns a boolean mask (True = exclude) for physiological FDG regions:
      - Brain:   top 12 % of Z axis (superior)
      - Thyroid: Z 78–90 %, central XY 35–65 %
      - Bladder: bottom 8 % of Z (inferior), central XY 35–65 %
    These fractions assume the image is head-to-toe (superior = high Z index).
    If the image is toe-to-head (origin inferior), invert Z fractions.
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

    # Brain — entire XY, top Z
    brain_z = int(z * (1 - brain_frac))
    mask[:, :, brain_z:] = True

    # Thyroid — central XY band, upper-neck Z
    tz_lo, tz_hi = int(z * thyroid_lo), int(z * thyroid_hi)
    tx_lo, tx_hi = int(x * xy_lo), int(x * xy_hi)
    ty_lo, ty_hi = int(y * xy_lo), int(y * xy_hi)
    mask[tx_lo:tx_hi, ty_lo:ty_hi, tz_lo:tz_hi] = True

    # Bladder — central XY, bottom Z
    bladder_z = int(z * bladder_frac)
    bx_lo, bx_hi = int(x * xy_lo), int(x * xy_hi)
    by_lo, by_hi = int(y * xy_lo), int(y * xy_hi)
    mask[bx_lo:bx_hi, by_lo:by_hi, :bladder_z] = True

    return mask


def _estimate_anatomical_region(centroid_voxel: list[float], shape: tuple) -> str:
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


def _generate_mip_pngs(
    suv_arr: np.ndarray, output_dir: str, colormap: str = "hot"
) -> list[dict]:
    """Generate axial, coronal, sagittal Maximum Intensity Projection PNGs."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import Normalize
        import matplotlib.cm as cm
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_mip")
        return []

    os.makedirs(output_dir, exist_ok=True)
    valid = suv_arr[suv_arr > 0]
    disp_max = min(float(np.percentile(valid, 99.5)), 10.0) if len(valid) > 0 else 1.0
    disp_max = max(disp_max, 0.1)

    artifacts = []
    view_axes = {"axial": 2, "coronal": 1, "sagittal": 0}

    for view_name, axis in view_axes.items():
        try:
            mip = np.max(suv_arr, axis=axis)
            fig, ax = plt.subplots(figsize=(5, 9), facecolor="black")
            ax.imshow(
                mip.T,
                cmap=colormap,
                vmin=0,
                vmax=disp_max,
                aspect="auto",
                origin="lower",
            )
            ax.axis("off")
            ax.set_title(f"{view_name.capitalize()} MIP", color="white", fontsize=9, pad=4)
            cbar = fig.colorbar(
                cm.ScalarMappable(norm=Normalize(0, disp_max), cmap=colormap),
                ax=ax, fraction=0.03, pad=0.02,
            )
            cbar.set_label("SUV", color="white", fontsize=8)
            cbar.ax.yaxis.set_tick_params(color="white", labelcolor="white")

            png_path = os.path.join(output_dir, f"mip_{view_name}.png")
            fig.savefig(png_path, dpi=120, bbox_inches="tight", facecolor="black")
            plt.close(fig)

            artifacts.append({
                "name": f"mip_{view_name}.png",
                "artifact_type": "mip_png",
                "local_path": png_path,
                "content_type": "image/png",
            })
        except Exception as e:
            logger.error("mip_png_view_failed", view=view_name, error=str(e))
            try:
                plt.close("all")
            except Exception:
                pass

    return artifacts


def _generate_fused_petct_pngs(
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    output_dir: str,
    colormap: str = "hot",
    alpha: float = 0.65,
) -> list[dict]:
    """Generate fused PET-on-CT overlay PNGs for axial, coronal, sagittal views."""
    try:
        from app.services.fused_image_service import generate_fused_png_bytes, VIEWS
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_fused")
        return []

    os.makedirs(output_dir, exist_ok=True)
    artifacts = []
    for view_name in VIEWS:
        try:
            png_bytes = generate_fused_png_bytes(suv_arr, ct_arr, view_name, colormap, alpha)
            png_path = os.path.join(output_dir, f"fused_{view_name}.png")
            with open(png_path, "wb") as fh:
                fh.write(png_bytes)
            artifacts.append({
                "name": f"fused_{view_name}.png",
                "artifact_type": "fused_png",
                "local_path": png_path,
                "content_type": "image/png",
            })
        except Exception as exc:
            logger.error("fused_png_view_failed", view=view_name, error=str(exc))

    return artifacts


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Pipeline(BasePipeline):
    """Whole-body FDG-PET/CT oncology pipeline.

    Supports two lesion-detection modes selected automatically at startup:

    DL mode  — SwinUNETR sliding-window segmentation when
               ``model.custom_pet_weights_path`` is set in inference_config.yaml.
               Input: 2-channel [SUV, CT_HU] tensor (or 1-channel SUV-only when
               ``model.in_channels=1``).  Output: softmax/sigmoid lesion mask.

    Threshold — PERCIST 1.0 SUV-threshold fallback when no weights are
                provided or when DL inference fails at runtime.
    """

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._cfg = yaml.safe_load(f)

        self._model = None          # SwinUNETR; None → threshold mode
        self._device = None         # torch.device
        self._model_version: str = "pet_ct_percist_v1.0.0"
        self._model_checksum: str = "n/a_threshold_based"

        weights_path = self._cfg.get("model", {}).get("custom_pet_weights_path")
        if weights_path:
            try:
                self._load_model(weights_path)
            except Exception as exc:
                logger.warning(
                    "swin_unetr_load_failed_using_threshold",
                    weights=weights_path,
                    error=str(exc),
                )

        # AutoPET3 (nnU-Net v2) tier — only when no SwinUNETR model is loaded.
        self._nnunet_predictor = None
        if self._model is None:
            nnunet_dir = self._cfg.get("model", {}).get("custom_pet_nnunet_dir")
            if nnunet_dir:
                try:
                    self._load_nnunet_model(nnunet_dir)
                except Exception as exc:
                    logger.warning(
                        "autopet3_nnunet_load_failed_using_threshold",
                        nnunet_dir=nnunet_dir,
                        error=str(exc),
                    )

    def _load_nnunet_model(self, model_dir: str) -> None:
        """Load an AutoPET3 / nnU-Netv2 trained-model folder via nnUNetPredictor."""
        import torch
        from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

        model_cfg = self._cfg.get("model", {})
        folds = tuple(model_cfg.get("nnunet_folds", [0]))
        checkpoint = model_cfg.get("nnunet_checkpoint", "checkpoint_final.pth")

        device_str = self._cfg.get("inference", {}).get("device", "auto")
        if device_str == "auto":
            device_str = "cuda" if torch.cuda.is_available() else "cpu"

        predictor = nnUNetPredictor(device=torch.device(device_str), allow_tqdm=False)
        predictor.initialize_from_trained_model_folder(
            model_dir, use_folds=folds, checkpoint_name=checkpoint
        )
        self._nnunet_predictor = predictor
        self._model_version = f"pet_ct_autopet3_nnunet_{Path(model_dir).name}"
        self._model_checksum = "nnunet_model_dir"
        logger.info(
            "autopet3_nnunet_loaded", nnunet_dir=model_dir, folds=folds, device=device_str
        )

    def _run_nnunet_inference(
        self,
        suv_arr: np.ndarray,
        ct_arr: np.ndarray | None,
        voxel_spacing: tuple[float, float, float],
    ) -> np.ndarray:
        """AutoPET3 nnU-Net sliding-window inference → binary lesion mask (int32).

        Channels are stacked in the configured order (AutoPET default CT, PET).
        Wrapped by the caller in try/except → PERCIST fallback, so a spacing/
        channel mismatch degrades safely rather than crashing the pipeline.
        """
        model_cfg = self._cfg.get("model", {})
        order = list(model_cfg.get("nnunet_channel_order", ["ct", "pet"]))

        channels: list[np.ndarray] = []
        for ch in order:
            if ch == "pet":
                channels.append(suv_arr.astype(np.float32))
            elif ch == "ct":
                if ct_arr is None:
                    raise ValueError("AutoPET3 nnU-Net needs a CT channel but no CT is available")
                channels.append(ct_arr.astype(np.float32))
            else:
                raise ValueError(f"unknown nnunet channel '{ch}'")

        vol = np.stack(channels, axis=0)  # (C, X, Y, Z)
        props = {"spacing": [float(s) for s in voxel_spacing]}
        seg = self._nnunet_predictor.predict_single_npy_array(vol, props, None, None, False)
        return (np.asarray(seg) > 0).astype(np.int32)

    @staticmethod
    def _resolve_autopet3_model_folder(model_dir: str | None) -> str | None:
        """Resolve the nnU-Net MODEL_FOLDER (the dir containing fold_X subdirs).

        Accepts either the model folder itself or a parent (e.g. the download
        target /model_cache/autopet3, under which the zip extracted a nested
        folder). Searches a couple of levels down. Returns the path or None.
        """
        if not model_dir:
            return None
        root = Path(model_dir)
        if not root.exists():
            return None

        def has_folds(p: Path) -> bool:
            return any((p / f"fold_{x}").is_dir() for x in (0, 1, 2, 3, 4, "all"))

        if has_folds(root):
            return str(root)
        # Breadth-limited search (download targets are small dirs).
        for depth1 in (d for d in root.iterdir() if d.is_dir()):
            if has_folds(depth1):
                return str(depth1)
            for depth2 in (d for d in depth1.iterdir() if d.is_dir()):
                if has_folds(depth2):
                    return str(depth2)
        return None

    def _run_autopet3(
        self,
        suv_arr: np.ndarray,
        ct_arr: np.ndarray | None,
        affine: np.ndarray,
        working_dir: str,
    ) -> np.ndarray | None:
        """AutoPET3 (Team LesionTracer) lesion segmentation via isolated subprocess.

        Writes CT (_0000) + SUV (_0001) NIfTIs, invokes
        scripts/run_autopet3_predict.py (which loads the autopet3 nnU-Net fork in
        its own process), and reads back the lesion mask. Returns an int32 mask or
        None on any failure / missing prerequisite so the caller falls back.

        Requires CT — the AutoPET3 model is a 2-channel [CT, PET] model.
        """
        import subprocess
        import sys

        cfg = self._cfg.get("model", {}).get("autopet3", {})
        if not cfg.get("enabled", False):
            return None
        model_dir = self._resolve_autopet3_model_folder(cfg.get("model_dir"))
        if model_dir is None:
            logger.warning("autopet3_model_dir_unresolved", configured=cfg.get("model_dir"))
            return None
        if ct_arr is None:
            logger.warning("autopet3_requires_ct_skipping")
            return None

        backend_root = Path(__file__).resolve().parents[3]
        fork_path = cfg.get("fork_path") or str(backend_root / "external" / "autopet3")
        if not Path(fork_path).exists():
            logger.warning("autopet3_fork_missing", fork_path=fork_path)
            return None
        runner = backend_root / "scripts" / "run_autopet3_predict.py"
        if not runner.exists():
            logger.warning("autopet3_runner_missing", runner=str(runner))
            return None

        in_dir = os.path.join(working_dir, "autopet3_in")
        out_dir = os.path.join(working_dir, "autopet3_out")
        os.makedirs(in_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        # nnU-Net channel convention: CT = _0000, PET (SUV) = _0001.
        nib.save(nib.Nifti1Image(ct_arr.astype(np.float32), affine),
                 os.path.join(in_dir, "case_0000.nii.gz"))
        nib.save(nib.Nifti1Image(suv_arr.astype(np.float32), affine),
                 os.path.join(in_dir, "case_0001.nii.gz"))

        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

        python_exe = cfg.get("python_executable") or sys.executable
        folds = ",".join(str(f) for f in cfg.get("folds", [0, 1, 2, 3, 4]))
        cmd = [
            python_exe, str(runner),
            "--input", in_dir,
            "--output", out_dir,
            "--model", str(model_dir),
            "--fork", fork_path,
            "--folds", folds,
            "--checkpoint", cfg.get("checkpoint_name", "checkpoint_final.pth"),
            "--device", device,
        ]
        logger.info("autopet3_subprocess_start", device=device, folds=folds, model_dir=model_dir)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=int(cfg.get("timeout_sec", 3600)),
            )
        except Exception as exc:
            logger.warning("autopet3_subprocess_error", error=str(exc))
            return None
        if proc.returncode != 0:
            logger.warning(
                "autopet3_subprocess_failed",
                returncode=proc.returncode,
                stderr=(proc.stderr or "")[-1500:],
            )
            return None

        # nnU-Net names the output after the input case → case.nii.gz
        out_path = os.path.join(out_dir, "case.nii.gz")
        if not os.path.exists(out_path):
            candidates = [f for f in os.listdir(out_dir) if f.endswith(".nii.gz")]
            if not candidates:
                logger.warning("autopet3_no_output_produced", out_dir=out_dir)
                return None
            out_path = os.path.join(out_dir, candidates[0])

        seg = nib.load(out_path).get_fdata()
        lesion_label = cfg.get("lesion_label", 1)
        mask = (np.rint(seg) == int(lesion_label)) if lesion_label is not None else (seg > 0)
        self._model_version = f"pet_ct_autopet3_lesiontracer_{Path(str(model_dir)).name}"
        self._model_checksum = "autopet3_modelfolder"
        logger.info("autopet3_complete", lesion_voxels=int(mask.sum()))
        return mask.astype(np.int32)

    # ── DL model management ──────────────────────────────────────────────────

    def _load_model(self, weights_path: str) -> None:
        """Load SwinUNETR weights and move model to the configured device."""
        import hashlib

        import torch
        from monai.networks.nets import SwinUNETR

        model_cfg = self._cfg.get("model", {})
        inf_cfg = self._cfg.get("inference", {})

        in_channels = model_cfg.get("in_channels", 2)
        out_channels = model_cfg.get("out_channels", 2)
        feature_size = model_cfg.get("feature_size", 48)
        roi_size = tuple(inf_cfg.get("roi_size", [96, 96, 96]))
        use_checkpoint = model_cfg.get("use_checkpoint", False)

        device_str = inf_cfg.get("device", "auto")
        if device_str == "auto":
            device_str = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = torch.device(device_str)

        model = SwinUNETR(
            img_size=roi_size,   # required in MONAI 1.4; deprecated in 1.5+
            in_channels=in_channels,
            out_channels=out_channels,
            feature_size=feature_size,
            use_checkpoint=use_checkpoint,
        )

        state = torch.load(weights_path, map_location="cpu", weights_only=True)
        # Support both raw state_dict and checkpoint dicts
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
        self._model_version = f"pet_ct_swinunetr_{Path(weights_path).stem}"

        logger.info(
            "swin_unetr_loaded",
            device=str(self._device),
            in_channels=in_channels,
            out_channels=out_channels,
            feature_size=feature_size,
            checksum=self._model_checksum,
        )

    def _run_dl_inference(
        self,
        suv_arr: np.ndarray,
        ct_arr: np.ndarray | None,
    ) -> np.ndarray:
        """Sliding-window SwinUNETR inference → binary lesion mask (int32)."""
        import torch
        import torch.nn.functional as F
        from monai.inferers import sliding_window_inference

        model_cfg = self._cfg.get("model", {})
        inf_cfg = self._cfg.get("inference", {})
        pre_cfg = self._cfg.get("preprocessing", {})

        in_channels = model_cfg.get("in_channels", 2)
        out_channels = model_cfg.get("out_channels", 2)
        roi_size = tuple(inf_cfg.get("roi_size", [96, 96, 96]))
        sw_batch_size = inf_cfg.get("sw_batch_size", 1)
        overlap = inf_cfg.get("overlap", 0.5)
        mode = inf_cfg.get("mode", "gaussian")
        dl_threshold = inf_cfg.get("dl_lesion_threshold", 0.5)

        # ── Normalise SUV ──────────────────────────────────────────────────
        clip_suv = float(pre_cfg.get("clip_suv_max", 20.0))
        suv_norm = (np.clip(suv_arr, 0.0, clip_suv) / clip_suv).astype(np.float32)

        # ── Build input tensor ─────────────────────────────────────────────
        if in_channels >= 2 and ct_arr is not None:
            clip_ct_min = float(pre_cfg.get("clip_ct_min", -1000.0))
            clip_ct_max = float(pre_cfg.get("clip_ct_max", 1000.0))
            ct_norm = (
                (np.clip(ct_arr, clip_ct_min, clip_ct_max) - clip_ct_min)
                / (clip_ct_max - clip_ct_min)
            ).astype(np.float32)
            vol = np.stack([suv_norm, ct_norm], axis=0)          # (2, X, Y, Z)
        else:
            # SUV-only: single channel; zero-fill remaining channels
            channels = [suv_norm] + [np.zeros_like(suv_norm)] * (in_channels - 1)
            vol = np.stack(channels, axis=0)                      # (C, X, Y, Z)

        input_tensor = torch.from_numpy(vol[np.newaxis]).to(self._device)  # (1,C,X,Y,Z)

        # ── Sliding window inference ───────────────────────────────────────
        with torch.no_grad():
            output = sliding_window_inference(
                inputs=input_tensor,
                roi_size=roi_size,
                sw_batch_size=sw_batch_size,
                predictor=self._model,
                overlap=overlap,
                mode=mode,
            )                                                      # (1, out_ch, X, Y, Z)

        # ── Threshold → binary mask ────────────────────────────────────────
        if out_channels >= 2:
            probs = F.softmax(output, dim=1)
            lesion_prob = probs[0, 1].cpu().numpy()               # class 1 = lesion
        else:
            lesion_prob = torch.sigmoid(output[0, 0]).cpu().numpy()

        return (lesion_prob >= dl_threshold).astype(np.int32)

    def _run_physiologic_organ_exclusion(
        self,
        ct_nifti_path: str,
        suv_shape: tuple,
        working_dir: str,
        supp_cfg: dict[str, Any],
    ) -> tuple[np.ndarray, list[str]] | None:
        """Anatomy-aware physiologic FDG exclusion mask via TotalSegmentator.

        Segments the configured organs on the (PET-grid) CT and ORs them into a
        boolean exclusion mask aligned with the SUV grid. Returns (mask, organs)
        or None on any failure so the caller can fall back to the geometric mask.
        """
        from totalsegmentator.python_api import totalsegmentator as ts_run

        task = supp_cfg.get("totalseg_task", "total")
        organs = list(supp_cfg.get("exclude_organs", []) or [])
        dilate = int(supp_cfg.get("dilate_voxels", 0))
        if not organs:
            return None

        import torch

        device = "gpu" if torch.cuda.is_available() else "cpu"
        ts_out = os.path.join(working_dir, "petct_physio_seg")
        os.makedirs(ts_out, exist_ok=True)

        # Weights are located via the TOTALSEG_WEIGHTS_PATH env var (set on the
        # worker) and downloaded there on first use — the python_api takes no
        # weights_dir argument.
        logger.info("physiologic_totalseg_start", task=task, device=device, organs=organs)
        ts_run(
            input=Path(ct_nifti_path),
            output=Path(ts_out),
            task=task,
            device=device,
            quiet=True,
            roi_subset=organs,
        )

        excl = np.zeros(suv_shape, dtype=bool)
        found: list[str] = []
        for organ in organs:
            organ_path = os.path.join(ts_out, f"{organ}.nii.gz")
            if not os.path.exists(organ_path):
                logger.warning("physiologic_organ_missing", organ=organ)
                continue
            mask = nib.load(organ_path).get_fdata() > 0.5
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

    def _segment_reference_organs(
        self, ct_nifti_path: str, suv_shape: tuple, working_dir: str
    ) -> dict[str, np.ndarray]:
        """Segment SUV reference organs (liver + aortic blood pool) via
        TotalSegmentator on the PET-grid CT.

        Returns a dict of ``{organ: bool mask}`` (at the SUV grid shape) for those
        successfully segmented. Returns ``{}`` on any failure, so the caller falls
        back to the HU-box heuristic. These masks are used only for reference SUV
        statistics — they are NOT added to the lesion-exclusion mask.
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

        # Run via the CLI in a separate subprocess. TotalSegmentator's nnU-Net
        # backend uses Python multiprocessing for preprocessing/export, which a
        # daemonic Celery worker process is forbidden from spawning ("daemonic
        # processes are not allowed to have children"). A subprocess (fork+exec of
        # a separate program) is exempt from that restriction — same isolation the
        # AutoPET3 tier uses.
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
                mask = nib.load(organ_path).get_fdata() > 0.5
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

    # ── Series classification ─────────────────────────────────────────────────

    def _classify_series(self, series: list[Series]) -> dict[str, Series]:
        """Pick the PET and CT series to fuse.

        A PET/CT study commonly carries several CT series — a single-slice scout
        (topogram/localizer) plus the volumetric whole-body CT. The scout's
        geometry does not overlap the PET volume, so choosing it makes the CT
        resample onto the PET grid come out entirely air (-1000 HU): a black CT
        background and broken reference-region stats. We therefore gather every
        candidate per modality, drop single-plane scouts, and keep the most
        voxel-rich (highest instance count) acquisition.
        """
        pet_candidates: list[Series] = []
        ct_candidates: list[Series] = []
        for s in series:
            desc = (s.series_description or "").strip()
            modality = (getattr(s, "modality", "") or "").upper()

            if modality == "PT" or any(re.search(p, desc) for p in _PET_PATTERNS):
                pet_candidates.append(s)
            elif modality == "CT" or any(re.search(p, desc) for p in _CT_PATTERNS):
                ct_candidates.append(s)

        def _instances(s: Series) -> int:
            return getattr(s, "num_instances", 0) or 0

        def _is_scout(s: Series) -> bool:
            desc = (s.series_description or "").strip()
            n = _instances(s)
            # A scout is either named like one, or is a *known* degenerate volume
            # (1–2 slices) that cannot form a 3-D CT overlapping the PET FOV.
            # n == 0 means the instance count is unknown (not populated) — do NOT
            # treat that as a scout, or every CT gets excluded when counts are
            # missing and the fallback would re-select the topogram.
            return any(re.search(p, desc) for p in _CT_SCOUT_PATTERNS) or (0 < n <= 2)

        classified: dict[str, Series] = {}

        if pet_candidates:
            # Prefer the most voxel-rich PET acquisition (skip any tiny derived series).
            classified["PET"] = max(pet_candidates, key=_instances)

        if ct_candidates:
            diagnostic = [s for s in ct_candidates if not _is_scout(s)]
            # Prefer a true volumetric CT; fall back to the largest of whatever
            # exists so we still attempt fusion rather than silently dropping CT.
            pool = diagnostic or ct_candidates
            classified["CT"] = max(pool, key=_instances)
            if not diagnostic:
                logger.warning(
                    "ct_series_only_scouts",
                    chosen=classified["CT"].series_description,
                    instances=_instances(classified["CT"]),
                )

        if "PET" not in classified and series:
            # Last resort: take the first series as PET
            classified["PET"] = series[0]

        return classified

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
        if "PET" not in classified:
            raise ValueError("No PET series found for pet_ct pipeline")

        if "CT" not in classified:
            qa_flags.append("no_ct_series")
            qa_details["ct_note"] = "CT series not found; reference region extraction will use fallback"
            logger.warning("no_ct_series_found", study_uid=study.study_instance_uid)

        # Download raw PET DICOMs to extract calibration tags
        pet_dicom_dir = os.path.join(working_dir, "pet_dicoms")
        os.makedirs(pet_dicom_dir, exist_ok=True)
        pet_dicoms: list[str] = loop.run_until_complete(
            pacs.download_series_dicoms(
                study.study_instance_uid,
                classified["PET"].series_instance_uid,
                pet_dicom_dir,
            )
        )

        if not pet_dicoms:
            raise ValueError("Failed to download PET DICOM files")

        # Extract SUV calibration from the first DICOM slice
        suv_params = _extract_suv_params(pet_dicoms[0])

        missing_cal: list[str] = []
        if suv_params["patient_weight_g"] <= 0:
            missing_cal.append("patient_weight")
        if suv_params["injected_dose_bq"] <= 0:
            missing_cal.append("injected_dose")
        if missing_cal:
            qa_flags.append("missing_calibration_data")
            qa_details["missing_calibration"] = missing_cal

        if self._cfg.get("quality_checks", {}).get("warn_on_missing_calibration", True):
            if suv_params.get("units") not in ("BQML", "BQ/ML"):
                qa_flags.append("non_bqml_units")
                qa_details["units"] = suv_params.get("units", "unknown")

        # Build SUV NIfTI from raw DICOMs
        nifti_dir = os.path.join(working_dir, "nifti")
        os.makedirs(nifti_dir, exist_ok=True)
        suv_nifti_path = os.path.join(nifti_dir, "pet_suv.nii.gz")
        raw_nifti_path = os.path.join(nifti_dir, "pet_raw.nii.gz")
        _build_suv_nifti(pet_dicom_dir, suv_params, suv_nifti_path, raw_output_path=raw_nifti_path)

        # QA: check image dimensions
        pet_img = nib.load(suv_nifti_path)
        pet_dims = pet_img.shape
        min_slices = self._cfg.get("quality_checks", {}).get("min_slices", 100)
        if max(pet_dims) < min_slices:
            qa_flags.append("insufficient_coverage")
            qa_details["pet_dimensions"] = list(pet_dims)

        # Download CT and resample to PET grid (if available)
        ct_nifti_path = None
        ct_original_path = None  # full-resolution CT (pre-resample) for accurate HU
        if "CT" in classified:
            ct_nifti_path = os.path.join(nifti_dir, "ct.nii.gz")
            try:
                loop.run_until_complete(
                    pacs.download_series_as_nifti(
                        study.study_instance_uid,
                        classified["CT"].series_instance_uid,
                        ct_nifti_path,
                    )
                )
                # Keep the original diagnostic-resolution CT for per-lesion HU
                # sampling (the PET-grid resample is too coarse — ~4 mm voxels —
                # and partial-volumes HU toward air near sinuses/airways).
                ct_original_path = ct_nifti_path
                # Resample CT to PET space (for fusion / reference ROIs / concordance)
                ct_pet_path = os.path.join(nifti_dir, "ct_in_pet_space.nii.gz")
                _resample_ct_to_pet(suv_nifti_path, ct_nifti_path, ct_pet_path)
                ct_nifti_path = ct_pet_path
            except Exception as exc:
                logger.warning("ct_download_or_resample_failed", error=str(exc))
                ct_nifti_path = None
                ct_original_path = None
                qa_flags.append("ct_registration_failed")

        logger.info(
            "pet_ct_preprocess_complete",
            study_uid=study.study_instance_uid,
            pet_dims=list(pet_dims),
            has_ct=ct_nifti_path is not None,
            suv_calibrated=_compute_suv_factor(suv_params) > 0,
            qa_flags=qa_flags,
        )

        return {
            "suv_nifti_path": suv_nifti_path,
            "raw_nifti_path": raw_nifti_path if os.path.exists(raw_nifti_path) else None,
            "ct_nifti_path": ct_nifti_path,
            "ct_original_nifti_path": ct_original_path,
            "suv_params": suv_params,
            "pet_dims": list(pet_dims),
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    # ── Phase 2: Infer ────────────────────────────────────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("pet_ct_inference_start")

        cfg_inf = self._cfg["inference"]
        cfg_post = self._cfg.get("postprocessing", {})

        suv_img = nib.load(preprocessed["suv_nifti_path"])
        suv_arr = suv_img.get_fdata().astype(np.float32)
        affine = suv_img.affine
        voxel_spacing = tuple(abs(float(affine[i, i])) for i in range(3))
        voxel_vol_ml = float(np.prod(voxel_spacing)) / 1000.0

        # Original full-resolution CT (for accurate per-lesion HU sampling). The
        # PET-grid CT is too coarse (~4 mm) for trustworthy density values.
        orig_ct_img = None
        orig_ct_path = preprocessed.get("ct_original_nifti_path")
        if orig_ct_path and os.path.exists(orig_ct_path):
            try:
                orig_ct_img = nib.load(orig_ct_path)
            except Exception as exc:
                logger.warning("original_ct_load_failed", error=str(exc))
                orig_ct_img = None

        # Reference region extraction
        ct_arr = None
        ref_stats: dict[str, dict[str, float]] = {}
        if preprocessed.get("ct_nifti_path"):
            ct_img = nib.load(preprocessed["ct_nifti_path"])
            ct_arr = ct_img.get_fdata().astype(np.float32)
            if ct_arr.shape == suv_arr.shape:
                # Prefer true organ segmentation (TotalSegmentator liver / aorta);
                # the HU-box heuristic averages generic 40–80 HU soft tissue over a
                # large region and badly under-reads the liver. Fall back to it only
                # for organs the segmentation misses.
                use_seg = cfg_inf.get("reference_organ_segmentation", True)
                if use_seg:
                    ref_masks = self._segment_reference_organs(
                        preprocessed["ct_nifti_path"], suv_arr.shape, working_dir
                    )
                    liver_stats = _suv_stats_from_mask(suv_arr, ref_masks.get("liver"))
                    aorta_stats = _suv_stats_from_mask(suv_arr, ref_masks.get("aorta"))
                    if liver_stats:
                        liver_stats["source"] = "totalseg_liver"
                        ref_stats["liver"] = liver_stats
                    if aorta_stats:
                        aorta_stats["source"] = "totalseg_aorta"
                        ref_stats["mediastinum"] = aorta_stats
                # Fill any organ the segmentation missed from the HU-box heuristic.
                if "liver" not in ref_stats or "mediastinum" not in ref_stats:
                    heur = _extract_reference_region_stats(suv_arr, ct_arr, cfg_post)
                    ref_stats.setdefault("liver", heur["liver"])
                    ref_stats.setdefault("mediastinum", heur["mediastinum"])
            else:
                logger.warning(
                    "ct_pet_shape_mismatch",
                    ct_shape=ct_arr.shape,
                    pet_shape=suv_arr.shape,
                )

        # Fallback if CT unavailable or extraction failed
        if not ref_stats:
            valid = suv_arr[suv_arr > 0.5]
            if len(valid) > 0:
                ref_stats["liver"] = {
                    "mean": float(np.percentile(valid, 65)),
                    "std": float(np.std(valid) * 0.25),
                    "n_voxels": 0,
                    "fallback": True,
                }
            else:
                ref_stats["liver"] = {"mean": 2.5, "std": 0.6, "n_voxels": 0, "fallback": True}
            ref_stats["mediastinum"] = {
                "mean": ref_stats["liver"]["mean"] * 0.5,
                "std": 0.2,
                "n_voxels": 0,
                "fallback": True,
            }

        liver_mean = ref_stats["liver"]["mean"]
        liver_std = ref_stats["liver"]["std"]
        med_mean = ref_stats["mediastinum"]["mean"]

        # ── Lesion segmentation: DL or threshold ──────────────────────────────
        suv_thresh_abs = cfg_inf.get("suv_threshold_absolute", 2.5)
        percist_factor = cfg_inf.get("percist_liver_factor", 1.5)
        percist_threshold = percist_factor * (liver_mean + 2.0 * liver_std)

        # (A) Calibration guard. When patient weight / injected dose are absent the
        # SUV NIfTI holds RELATIVE-normalised intensities (see _build_suv_nifti
        # fallback), so the absolute SUV threshold is invalid and would flag a
        # large fraction of the body. Fall back to the liver-relative PERCIST
        # threshold (which adapts to the data's own scale) and mark the result
        # non-quantitative downstream.
        suv_params = preprocessed.get("suv_params", {})
        suv_calibrated = _compute_suv_factor(suv_params) > 0
        if not suv_calibrated and cfg_inf.get("uncalibrated_use_relative_threshold", True):
            threshold = max(percist_threshold, 0.1)
            logger.warning(
                "suv_uncalibrated_relative_threshold",
                relative_threshold=round(threshold, 3),
            )
        else:
            # Calibrated SUV: detection threshold is either the case's own liver
            # SUVmean (case-adaptive, scales with each patient's physiologic
            # background) or a fixed absolute SUV cutoff. Liver-mean is preferred
            # but falls back to the absolute cutoff when no liver reference exists.
            thr_source = cfg_inf.get("calibrated_threshold_source", "liver_mean")
            if thr_source == "liver_mean" and liver_mean > 0:
                liver_factor = float(cfg_inf.get("calibrated_liver_factor", 1.0))
                threshold = max(liver_factor * liver_mean, 0.1)
                logger.info(
                    "calibrated_liver_mean_threshold",
                    liver_mean=round(liver_mean, 3),
                    liver_factor=liver_factor,
                    effective_threshold=round(threshold, 3),
                )
            else:
                threshold = suv_thresh_abs  # fixed absolute SUV cutoff

        # DL tier precedence: AutoPET3 (isolated subprocess) → SwinUNETR
        # (in-process) → generic nnU-Net (in-process) → PERCIST threshold.
        inference_method: str | None = None
        raw_mask = None

        autopet3_cfg = self._cfg.get("model", {}).get("autopet3", {})
        if autopet3_cfg.get("enabled", False) and autopet3_cfg.get("model_dir"):
            try:
                logger.info("autopet3_inference_start")
                m = self._run_autopet3(suv_arr, ct_arr, affine, working_dir)
                if m is not None and m.shape == suv_arr.shape:
                    raw_mask = m
                    inference_method = "autopet3"
                    logger.info("autopet3_inference_complete", raw_lesion_voxels=int(raw_mask.sum()))
                elif m is not None:
                    logger.warning(
                        "autopet3_shape_mismatch",
                        got=tuple(m.shape), expected=tuple(suv_arr.shape),
                    )
            except Exception as exc:
                logger.warning("autopet3_failed_falling_back", error=str(exc))

        if raw_mask is None and self._model is not None:
            try:
                logger.info(
                    "dl_lesion_inference_start",
                    model_version=self._model_version,
                    device=str(self._device),
                )
                raw_mask = self._run_dl_inference(suv_arr, ct_arr)
                inference_method = "swin_unetr"
                logger.info(
                    "dl_lesion_inference_complete",
                    raw_lesion_voxels=int(raw_mask.sum()),
                )
            except Exception as exc:
                logger.warning(
                    "dl_inference_failed_falling_back_to_threshold",
                    error=str(exc),
                )
                raw_mask = (suv_arr >= threshold).astype(np.int32)
                inference_method = "threshold_fallback"
        elif raw_mask is None and self._nnunet_predictor is not None:
            try:
                logger.info("nnunet_inference_start", model_version=self._model_version)
                raw_mask = self._run_nnunet_inference(suv_arr, ct_arr, voxel_spacing)
                inference_method = "nnunet"
                # generic in-process nnU-Net glue (channel order / spacing
                # orientation) requires on-site validation; surface that.
                preprocessed.setdefault("qa_flags", []).append("dl_nnunet_unvalidated")
                logger.info("nnunet_inference_complete", raw_lesion_voxels=int(raw_mask.sum()))
            except Exception as exc:
                logger.warning("nnunet_failed_falling_back_to_threshold", error=str(exc))
                raw_mask = (suv_arr >= threshold).astype(np.int32)
                inference_method = "threshold_fallback"
        elif raw_mask is None:
            logger.info(
                "suv_threshold",
                liver_mean=round(liver_mean, 3),
                liver_std=round(liver_std, 3),
                percist_reference=round(percist_threshold, 3),
                effective_threshold=round(threshold, 3),
            )
            raw_mask = (suv_arr >= threshold).astype(np.int32)
            inference_method = "threshold"

        # Apply physiologic exclusion, connected-component labelling.
        # Threshold detection lights up all FDG-avid tissue, so suppress normal
        # uptake: prefer anatomy-aware organ masking (TotalSegmentator on the CT)
        # and fall back to the coarse geometric mask. A trained DL model is
        # trusted to discriminate physiologic uptake itself, so it keeps the
        # original geometric behaviour.
        supp_cfg = cfg_inf.get("physiologic_suppression", {})
        excl_mask = None
        suppression_method = "geometric"
        excluded_organs: list[str] = []
        if (
            inference_method in ("threshold", "threshold_fallback")
            and supp_cfg.get("enabled", True)
            and preprocessed.get("ct_nifti_path")
        ):
            try:
                result = self._run_physiologic_organ_exclusion(
                    preprocessed["ct_nifti_path"], suv_arr.shape, working_dir, supp_cfg
                )
                if result is not None:
                    excl_mask, excluded_organs = result
                    suppression_method = "totalsegmentator"
            except Exception as exc:
                logger.warning("physiologic_organ_exclusion_failed", error=str(exc))
                excl_mask = None
        if excl_mask is None:
            excl_mask = _build_physiological_exclusion_mask(suv_arr.shape, cfg_post)
        raw_mask[excl_mask] = 0
        labeled, n_components = ndimage.label(raw_mask)
        lesion_mask = raw_mask

        # CT concordance: a true tumour has a soft-tissue correlate on CT;
        # physiologic uptake in hollow organs (bowel gas) does not.
        conc_cfg = cfg_inf.get("ct_concordance", {})
        conc_enabled = bool(conc_cfg.get("enabled", True)) and ct_arr is not None
        # Safety guard: if the CT is degenerate (e.g. failed PET/CT
        # co-registration → effectively all air), concordance cannot discriminate
        # and would wrongly reject EVERY focus (including a true tumour). Disable
        # it in that case rather than nuke all detections.
        if conc_enabled:
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
        n_rejected_concordance = 0

        min_vol_ml = cfg_inf.get("min_lesion_volume_ml", 1.2)
        sphere_r_mm = cfg_inf.get("suv_peak_sphere_radius_mm", 6.204)

        # (B) Oversized-focus sanity bound. A single connected component bigger
        # than this absolute volume — or this fraction of the whole imaged volume
        # — is not a real lesion; it is diffuse uptake the suppression stages
        # failed to break up. Drop it and count it for a QA flag.
        max_lesion_ml = cfg_inf.get("max_lesion_volume_ml", 2000.0)
        max_lesion_frac = cfg_inf.get("max_lesion_volume_fraction", 0.10)
        total_volume_ml = float(suv_arr.size) * voxel_vol_ml
        max_frac_ml = max_lesion_frac * total_volume_ml
        n_rejected_oversize = 0

        lesions: list[dict[str, Any]] = []
        for comp_id in range(1, n_components + 1):
            comp_mask = (labeled == comp_id)
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
            suv_peak = _compute_suv_peak(
                suv_arr, comp_mask, voxel_vol_ml, sphere_r_mm, voxel_spacing
            )
            tlg = suv_mean * vol_ml  # g (since SUV is dimensionless and vol in mL ≈ g)
            centroid = ndimage.center_of_mass(comp_mask)

            # CT density (HU) over the focus core — the anatomical (CT) correlate
            # of the metabolic focus. Soft tissue ~30–60 HU; fat negative;
            # bone/calcium high. Sampled on the ORIGINAL diagnostic CT (median over
            # the eroded core) so it tracks what a reader measures in the viewer;
            # falls back to the coarse PET-grid CT mean only if the original is
            # unavailable. None when no co-registered CT exists.
            ct_mean_hu = None
            if orig_ct_img is not None:
                ct_mean_hu = _ct_hu_on_original(orig_ct_img, comp_mask, affine)
            if ct_mean_hu is None and ct_arr is not None and ct_arr.shape == suv_arr.shape:
                comp_ct = ct_arr[comp_mask]
                if comp_ct.size > 0:
                    ct_mean_hu = round(float(np.mean(comp_ct)), 1)

            lesions.append({
                "id": len(lesions) + 1,
                "suv_max": round(suv_max, 2),
                "suv_mean": round(suv_mean, 2),
                "suv_peak": round(suv_peak, 2),
                # Tumor-to-Liver Ratio: lesion SUVmax / liver SUVmean reference.
                "tlr": round(suv_max / liver_mean, 2) if liver_mean > 0 else None,
                "volume_ml": round(vol_ml, 2),
                "ct_mean_hu": ct_mean_hu,
                "tlg": round(tlg, 2),
                "anatomical_region": _estimate_anatomical_region(
                    [round(c, 1) for c in centroid], suv_arr.shape
                ),
                "centroid_voxel": [round(c, 1) for c in centroid],
            })

        lesions.sort(key=lambda x: x["suv_max"], reverse=True)

        logger.info(
            "pet_ct_inference_complete",
            inference_method=inference_method,
            suppression_method=suppression_method,
            excluded_organs=excluded_organs,
            rejected_non_concordant=n_rejected_concordance,
            rejected_oversize=n_rejected_oversize,
            suv_calibrated=suv_calibrated,
            threshold=round(threshold, 2),
            lesion_count=len(lesions),
            total_mtv=round(sum(x["volume_ml"] for x in lesions), 1),
        )

        return {
            "lesions": lesions,
            "suppression_method": suppression_method,
            "excluded_organs": excluded_organs,
            "rejected_non_concordant": n_rejected_concordance,
            "rejected_oversize": n_rejected_oversize,
            "suv_calibrated": suv_calibrated,
            "lesion_mask_array": (labeled > 0).astype(np.uint8),
            "suv_array": suv_arr,
            "ct_array": ct_arr if preprocessed.get("ct_nifti_path") else None,
            "affine": affine,
            "voxel_spacing_mm": voxel_spacing,
            "voxel_vol_ml": voxel_vol_ml,
            "percist_threshold": round(threshold, 3),
            "reference_regions": ref_stats,
            "liver_mean": liver_mean,
            "liver_std": liver_std,
            "mediastinum_mean": med_mean,
            "inference_method": inference_method,
            **{k: v for k, v in preprocessed.items() if k != "suv_array"},
        }

    # ── Phase 3: Postprocess ──────────────────────────────────────────────────

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("pet_ct_postprocess_start")

        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        lesions: list[dict] = inference_output["lesions"]
        suv_arr: np.ndarray = inference_output["suv_array"]
        ct_arr: np.ndarray | None = inference_output.get("ct_array")
        affine = inference_output["affine"]
        lesion_mask: np.ndarray = inference_output["lesion_mask_array"]
        voxel_spacing = inference_output["voxel_spacing_mm"]
        percist_threshold = inference_output["percist_threshold"]
        ref_regions = inference_output["reference_regions"]
        liver_mean = inference_output["liver_mean"]
        liver_std = inference_output["liver_std"]
        med_mean = inference_output["mediastinum_mean"]
        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))
        suv_params = inference_output.get("suv_params", {})
        suv_calibrated = bool(inference_output.get("suv_calibrated", True))
        n_rejected_oversize = int(inference_output.get("rejected_oversize", 0) or 0)
        ct_available = ct_arr is not None

        total_mtv = round(sum(x["volume_ml"] for x in lesions), 2)
        total_tlg = round(sum(x["tlg"] for x in lesions), 2)

        # Deauville: use the hottest lesion
        global_suv_max = max((x["suv_max"] for x in lesions), default=0.0)
        deauville = _deauville_score(global_suv_max, med_mean, liver_mean)
        # Tumor-to-Liver Ratio for the hottest lesion (SUVmax / liver SUVmean).
        tumor_to_liver_ratio = (
            round(global_suv_max / liver_mean, 2) if liver_mean > 0 and lesions else None
        )

        # (C) Result confidence. SUV-based reporting is only quantitative when the
        # tracer dose / patient weight are present; without CT the physiologic
        # suppression and concordance safeguards are degraded. Surface this so the
        # UI/report can show it instead of a confident positive on weak data.
        confidence_reasons: list[str] = []
        if not suv_calibrated:
            confidence_reasons.append(
                "SUV not calibrated (missing patient weight / injected dose) — "
                "displayed values are relative FDG intensities, not quantitative SUV"
            )
            if "suv_non_quantitative" not in qa_flags:
                qa_flags.append("suv_non_quantitative")
        if not ct_available:
            confidence_reasons.append(
                "CT unavailable — physiologic-uptake suppression and CT "
                "concordance filtering were disabled"
            )
        if n_rejected_oversize > 0:
            confidence_reasons.append(
                f"{n_rejected_oversize} oversized focus/foci rejected as "
                "non-physiologic diffuse uptake (not reported as lesions)"
            )
            if "oversized_lesion_rejected" not in qa_flags:
                qa_flags.append("oversized_lesion_rejected")
            qa_details["oversized_lesions_rejected"] = n_rejected_oversize
        result_confidence = "low" if confidence_reasons else "standard"

        # PERCIST response categories require a prior scan.
        # At baseline, report descriptive status only.
        if not lesions:
            percist_score = "No Active Disease"
        else:
            percist_score = "Active Disease (Baseline)"

        # Tracer display name
        radiopharmaceutical = suv_params.get("tracer_name", suv_params.get("radionuclide", "FDG"))

        # QA: check max SUV for unrealistic values
        max_suv_limit = self._cfg.get("quality_checks", {}).get("max_expected_suv", 50.0)
        if global_suv_max > max_suv_limit:
            qa_flags.append("suv_range_suspicious")
            qa_details["suv_max_observed"] = round(global_suv_max, 1)

        # Save SUV NIfTI artifact
        suv_artifact_path = os.path.join(artifacts_dir, "pet_suv.nii.gz")
        nib.save(nib.Nifti1Image(suv_arr, affine), suv_artifact_path)

        # Save segmentation NIfTI
        seg_path = os.path.join(artifacts_dir, "lesion_mask.nii.gz")
        nib.save(nib.Nifti1Image(lesion_mask, affine), seg_path)

        # Save report JSON
        report_data = {
            "lesions": lesions,
            "percist_threshold": percist_threshold,
            "reference_regions": {
                k: {kk: round(vv, 4) for kk, vv in v.items() if isinstance(vv, float)}
                for k, v in ref_regions.items()
            },
        }
        report_path = os.path.join(artifacts_dir, "report.json")
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)

        # Generate MIP PNGs
        mip_artifacts: list[dict] = []
        if self._cfg.get("postprocessing", {}).get("generate_mip", True):
            colormap = self._cfg.get("postprocessing", {}).get("mip_colormap", "hot")
            mip_artifacts = _generate_mip_pngs(suv_arr, artifacts_dir, colormap)

        # Generate fused PET/CT PNGs
        fused_artifacts: list[dict] = []
        if self._cfg.get("postprocessing", {}).get("generate_fused", True):
            colormap = self._cfg.get("postprocessing", {}).get("mip_colormap", "hot")
            fused_artifacts = _generate_fused_petct_pngs(suv_arr, ct_arr, artifacts_dir, colormap)

        # Copy CT NIfTI as an artifact if available
        ct_nifti_path = inference_output.get("ct_nifti_path")
        ct_artifacts = []
        if ct_nifti_path and os.path.exists(ct_nifti_path):
            import shutil
            ct_artifact_path = os.path.join(artifacts_dir, "ct.nii.gz")
            shutil.copy2(ct_nifti_path, ct_artifact_path)
            ct_artifacts = [{
                "name": "ct",
                "artifact_type": "ct_nifti",
                "local_path": ct_artifact_path,
                "content_type": "application/gzip",
            }]

        # Copy raw PET (Bq/mL) NIfTI as an artifact if available. Used for display
        # rendering (PET pane + fused view) so the viewer shows acquired PET
        # intensities rather than SUV-normalised values.
        raw_nifti_path = inference_output.get("raw_nifti_path")
        raw_pet_artifacts = []
        if raw_nifti_path and os.path.exists(raw_nifti_path):
            import shutil
            raw_artifact_path = os.path.join(artifacts_dir, "pet_raw.nii.gz")
            shutil.copy2(raw_nifti_path, raw_artifact_path)
            raw_pet_artifacts = [{
                "name": "pet_raw",
                "artifact_type": "pet_nifti",
                "local_path": raw_artifact_path,
                "content_type": "application/gzip",
            }]

        inference_method = inference_output.get("inference_method", "threshold")
        suppression_method = inference_output.get("suppression_method", "geometric")
        excluded_organs = inference_output.get("excluded_organs", []) or []
        rejected_non_concordant = int(inference_output.get("rejected_non_concordant", 0) or 0)

        # Tumor-positive call: key off the case's liver SUVmean (physiologic
        # background) rather than a fixed absolute SUV cutoff, so the call adapts
        # to each patient's normal liver uptake. Falls back to the absolute SUV
        # cutoff when liver-mean mode is off or no liver reference is available.
        cfg_inf = self._cfg.get("inference", {})
        if cfg_inf.get("calibrated_threshold_source", "liver_mean") == "liver_mean" and liver_mean > 0:
            diag_factor = float(cfg_inf.get("diagnosis_liver_factor", 1.0))
            diag_cutoff = diag_factor * liver_mean
            diag_label = "liver SUVmean" if diag_factor == 1.0 else f"liver SUVmean × {diag_factor:g}"
        else:
            diag_cutoff = float(cfg_inf.get("suv_threshold_absolute", 2.5))
            diag_label = "SUV"
        diagnosis = _derive_diagnosis(lesions, deauville, diag_cutoff, diag_label)
        if not suv_calibrated:
            diagnosis = (
                "NON-QUANTITATIVE (uncalibrated SUV) — interpret uptake relatively, "
                "not by absolute SUV. " + diagnosis
            )
        processing_notes = self._build_notes(
            lesions, qa_flags, percist_threshold, deauville, inference_method,
            suppression_method=suppression_method,
            excluded_organs=excluded_organs,
            rejected_non_concordant=rejected_non_concordant,
            suv_calibrated=suv_calibrated,
            rejected_oversize=n_rejected_oversize,
        )

        result = {
            "summary": {
                "lesions_detected": len(lesions) > 0,
                "lesion_count": len(lesions),
                "mtv_total_ml": total_mtv,
                "tlg_total": total_tlg,
                "suvmax_body": round(global_suv_max, 2),
                "radiopharmaceutical": radiopharmaceutical,
                "percist_score": percist_score,
                "deauville_score": deauville if lesions else None,
                "tumor_to_liver_ratio": tumor_to_liver_ratio,
                "diagnosis": diagnosis,
                "inference_method": inference_method,
                "quantitative": suv_calibrated,
                "confidence": result_confidence,
                "confidence_reasons": confidence_reasons,
                "processing_notes": processing_notes,
            },
            "measurements": {
                "lesions": lesions,
                "reference_organs": {
                    "liver_suv_mean": round(liver_mean, 3),
                    "liver_suv_sd": round(liver_std, 3),
                    "mediastinum_suv_mean": round(med_mean, 3),
                },
                "whole_body": {
                    "mtv_total_ml": total_mtv,
                    "tlg_total": total_tlg,
                    "suvmax_body": round(global_suv_max, 2),
                    "lesion_count": len(lesions),
                },
                "voxel_spacing_mm": [round(s, 3) for s in voxel_spacing],
                "image_dimensions": list(suv_arr.shape),
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": self._model_version,
            "model_checksum": self._model_checksum,
            "artifacts": [
                {
                    "name": "pet_suv",
                    "artifact_type": "pet_nifti",
                    "local_path": suv_artifact_path,
                    "content_type": "application/gzip",
                },
                {
                    "name": "lesion_mask",
                    "artifact_type": "segmentation_nifti",
                    "local_path": seg_path,
                    "content_type": "application/gzip",
                },
                {
                    "name": "report",
                    "artifact_type": "report_json",
                    "local_path": report_path,
                    "content_type": "application/json",
                },
                *mip_artifacts,
                *fused_artifacts,
                *ct_artifacts,
                *raw_pet_artifacts,
            ],
        }

        logger.info(
            "pet_ct_postprocess_complete",
            lesion_count=len(lesions),
            total_mtv=total_mtv,
            deauville=deauville,
            percist=percist_score,
            qa_flags=qa_flags,
        )

        return result

    @staticmethod
    def _build_notes(
        lesions: list[dict], qa_flags: list[str],
        threshold: float, deauville: int,
        inference_method: str = "threshold",
        suppression_method: str = "geometric",
        excluded_organs: list[str] | None = None,
        rejected_non_concordant: int = 0,
        suv_calibrated: bool = True,
        rejected_oversize: int = 0,
    ) -> str:
        parts: list[str] = []

        if not suv_calibrated:
            parts.append(
                "NON-QUANTITATIVE: SUV calibration unavailable — a relative "
                "(liver-referenced) threshold was used and absolute SUV values "
                "are not reliable."
            )

        method_label = {
            "autopet3": "AutoPET3 (Team LesionTracer) nnU-Net deep-learning segmentation",
            "swin_unetr": "SwinUNETR deep-learning segmentation",
            "nnunet": "nnU-Net deep-learning segmentation",
            "threshold_fallback": "PERCIST SUV-threshold (DL fallback)",
            "threshold": "PERCIST SUV-threshold",
        }.get(inference_method, inference_method)
        parts.append(f"Detection method: {method_label}.")
        if inference_method == "nnunet":
            parts.append(
                "nnU-Net output is pending on-site validation "
                "(channel order / spacing orientation) — verify before clinical use."
            )

        if rejected_oversize > 0:
            parts.append(
                f"Rejected {rejected_oversize} oversized focus/foci exceeding the "
                "single-lesion volume bound (diffuse uptake, not a discrete lesion)."
            )

        # Physiologic-uptake suppression transparency (threshold detection only).
        if "threshold" in inference_method:
            if suppression_method == "totalsegmentator" and excluded_organs:
                parts.append(
                    "Physiologic suppression: anatomy-aware (TotalSegmentator) — excluded "
                    + ", ".join(o.replace("_", " ") for o in excluded_organs) + "."
                )
            else:
                parts.append(
                    "Physiologic suppression: geometric brain/thyroid/bladder mask "
                    "(CT organ segmentation unavailable)."
                )
            if rejected_non_concordant > 0:
                parts.append(
                    f"Rejected {rejected_non_concordant} focus/foci lacking a CT "
                    "soft-tissue correlate (e.g. bowel gas)."
                )

        if lesions:
            total_mtv = sum(x["volume_ml"] for x in lesions)
            parts.append(
                f"Detected {len(lesions)} FDG-avid lesion(s) "
                + (f"above SUV {threshold:.2f}. " if "threshold" in inference_method else ". ")
                + f"Total MTV: {total_mtv:.1f} mL. Highest Deauville score: {deauville}."
            )
        else:
            parts.append("No FDG-avid lesions detected.")
        if "missing_calibration_data" in qa_flags:
            parts.append("Warning: SUV calibration data partially missing from DICOM header.")
        if "no_ct_series" in qa_flags:
            parts.append("CT series unavailable; reference region extracted via global SUV fallback.")
        if inference_method == "threshold_fallback":
            parts.append("Note: DL model inference failed; results use threshold fallback.")
        return " ".join(parts)
