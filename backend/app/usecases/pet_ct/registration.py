"""Stage 2 — World-coordinate alignment and PET/CT geometry.

This module owns *all* spatial reasoning for the PET/CT pipeline:

1.  **DICOM → calibrated SUV volume** (metadata parsing, decay correction, SUV
    factor, NIfTI construction) — moved verbatim from the original monolith so
    the quantitative pathway is byte-for-byte unchanged.
2.  **Volumetric co-registration** — resampling the CT (and label maps) onto the
    PET voxel grid via SimpleITK. PET/CT scanners are inherently co-registered,
    so a single world-coordinate resample yields voxel-aligned volumes for the
    reference-region stats, concordance, and fused rendering.
3.  **Per-lesion world-coordinate slice matching** (the new methodology) — for
    each detected lesion, locate its peak-SUV *epicenter*, convert that voxel to
    a physical world coordinate (mm) through the volume's affine, and find the
    CT slice whose ``ImagePositionPatient`` z is the closest match. This makes
    display-slice selection robust to PET/CT slice-thickness mismatch (CT
    ~1–2 mm / 600 slices vs PET ~3–5 mm / 200 slices) instead of assuming a 1:1
    array-index correspondence, which would misalign anatomy or crash.

Array-axis convention
---------------------
Throughout this codebase the NIfTI volumes are indexed **(X, Y, Z)** — i.e. the
superior/inferior (slice) axis is **axis 2**, not axis 0. This is the opposite of
the ``(p_z, p_y, p_x)`` ordering used in bare pydicom pixel arrays. The world
transform below is expressed through the affine, so it is correct regardless of
ordering; where an explicit axis is needed the Z axis is index 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import pydicom
import SimpleITK as sitk
import structlog

logger = structlog.get_logger(__name__)

# F-18 physical half-life (109.77 min). Default when the DICOM header omits it.
F18_HALF_LIFE_SEC = 6586.2


# ── DICOM / SUV helpers (moved verbatim from pipeline.py) ───────────────────────

def parse_dicom_time(time_str: str) -> float:
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


def extract_suv_params(dicom_path: str) -> dict[str, Any]:
    """Read SUV calibration parameters from a PET DICOM file header."""
    ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)

    params: dict[str, Any] = {
        "units": "",
        "patient_weight_g": 0.0,
        "injected_dose_bq": 0.0,
        "half_life_sec": F18_HALF_LIFE_SEC,
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
            params["injection_time_sec"] = parse_dicom_time(
                str(rp.RadiopharmaceuticalStartTime)
            )
        nuc_seq = getattr(rp, "RadionuclideCodeSequence", None)
        if nuc_seq and len(nuc_seq) > 0:
            params["radionuclide"] = str(getattr(nuc_seq[0], "CodeMeaning", "18F"))

        tracer_name = str(getattr(rp, "Radiopharmaceutical", "") or "")
        if tracer_name:
            params["tracer_name"] = tracer_name

    acq_time = getattr(ds, "AcquisitionTime", None) or getattr(ds, "SeriesTime", None)
    if acq_time:
        params["scan_time_sec"] = parse_dicom_time(str(acq_time))

    return params


def compute_suv_factor(params: dict[str, Any]) -> float:
    """Return the multiplier to convert Bq/mL pixel values to SUV."""
    weight_g = params.get("patient_weight_g", 0.0)
    injected_bq = params.get("injected_dose_bq", 0.0)
    if weight_g <= 0 or injected_bq <= 0:
        return 0.0

    half_life_sec = params.get("half_life_sec", F18_HALF_LIFE_SEC)
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


def build_suv_nifti(
    dicom_dir: str, suv_params: dict, output_path: str, raw_output_path: str | None = None
) -> str:
    """Convert a PET DICOM series directory to a calibrated SUV NIfTI.

    Uses SimpleITK to read the DICOM series (preserves geometry), which already
    applies each slice's rescale slope/intercept (the modality LUT), so the array
    is ALREADY activity concentration in Bq/mL — do NOT reapply slope/intercept
    or the SUV is double-scaled. Multiplies by the SUV factor to get SUV.

    When ``raw_output_path`` is given, the raw PET activity volume (Bq/mL, before
    SUV normalisation) is also written with identical geometry, for display
    rendering (the viewer shows acquired PET intensities; SUV drives quantitation).
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

    arr = sitk.GetArrayFromImage(img).astype(np.float32)  # (Z, Y, X) / (Y, X) / (Z, Y, X, C)

    # Normalize to 3D (Z, Y, X)
    if arr.ndim == 2:
        arr = arr[np.newaxis, :, :]
    elif arr.ndim == 4:
        n_ch = arr.shape[-1]
        logger.warning("pet_dicom_multichannel", original_shape=list(arr.shape), channels=n_ch)
        if n_ch == 1:
            arr = arr[..., 0]
        elif n_ch == 3:
            arr = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
        else:
            arr = arr.mean(axis=-1)
    elif arr.ndim != 3:
        raise ValueError(f"Unsupported PET array shape: {arr.shape}")

    bqml = np.clip(arr, 0.0, None)

    suv_factor = compute_suv_factor(suv_params)
    if suv_factor > 0:
        suv = bqml * suv_factor
    else:
        # Fallback: relative normalization when calibration data absent.
        p99 = float(np.percentile(bqml[bqml > 0], 99)) if np.any(bqml > 0) else 1.0
        suv = bqml / (p99 / 8.0)
        logger.warning("suv_calibration_fallback", reason="missing_weight_or_dose")

    # Build a SimpleITK image from the SUV array and inherit the PET DICOM
    # geometry, then write with sitk (identical path to the CT conversion) so the
    # SUV NIfTI and the CT NIfTI share a consistent world frame.
    suv_img = sitk.GetImageFromArray(suv)   # (Z, Y, X) — SITK index order
    suv_img.SetSpacing(img.GetSpacing())
    suv_img.SetOrigin(img.GetOrigin())
    suv_img.SetDirection(img.GetDirection())
    sitk.WriteImage(suv_img, output_path)

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


def resample_ct_to_pet(pet_path: str, ct_path: str, output_path: str) -> str:
    """Resample CT to the PET voxel grid (PET/CT scanners are co-registered).

    This is the volumetric world-coordinate alignment that guarantees the CT and
    SUV arrays share indices — the basis for reference-region stats, CT
    concordance, and fused rendering. SimpleITK handles the PET/CT slice-thickness
    mismatch correctly because it resamples in physical (mm) space, not by index.
    """
    pet_img = sitk.ReadImage(pet_path, sitk.sitkFloat32)
    ct_img = sitk.ReadImage(ct_path, sitk.sitkFloat32)

    resampler = sitk.ResampleImageFilter()
    resampler.SetReferenceImage(pet_img)
    resampler.SetInterpolator(sitk.sitkLinear)
    resampler.SetDefaultPixelValue(-1000.0)
    ct_resampled = resampler.Execute(ct_img)
    sitk.WriteImage(ct_resampled, output_path)
    return output_path


def resample_labelmap_to_reference(label_path: str, reference_path: str, output_path: str) -> str:
    """Resample an integer label map onto another image's grid via nearest-neighbor.

    TotalSegmentator label maps are categorical (label IDs), so linear/spline
    interpolation would blend adjacent IDs into meaningless fractional values.
    Nearest-neighbor is the only interpolator that preserves discrete labels.
    """
    ref_img = sitk.ReadImage(reference_path, sitk.sitkFloat32)
    label_img = sitk.ReadImage(label_path, sitk.sitkInt32)

    resampler = sitk.ResampleImageFilter()
    resampler.SetReferenceImage(ref_img)
    resampler.SetInterpolator(sitk.sitkNearestNeighbor)
    resampler.SetDefaultPixelValue(0)
    resampled = resampler.Execute(label_img)
    sitk.WriteImage(resampled, output_path)
    return output_path


# ── World-coordinate per-lesion slice matching (Stage 2, new) ──────────────────

@dataclass
class SliceGeometry:
    """Physical geometry of a stack of parallel DICOM slices, along the slice axis.

    ``slice_world_z`` is the sorted list of each slice's physical z coordinate
    (``ImagePositionPatient[2]``, mm). ``index_by_sort`` maps a position in the
    sorted list back to the original acquisition index. We keep both so a matched
    world-z can be reported either as a physical coordinate or an array index.
    """

    slice_world_z: np.ndarray                 # (N,) sorted ascending
    origin_z_mm: float = 0.0                   # z of the first (lowest) slice
    slice_thickness_mm: float = 0.0
    n_slices: int = 0
    order: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))

    def nearest_index(self, world_z_mm: float) -> int:
        """Index (into the sorted slice stack) whose world z is closest to ``world_z_mm``.

        This is the crux of Stage 2: rather than assuming PET index N maps to CT
        index N (false when slice thicknesses differ), we find the CT slice that
        physically sits at the same z as the PET lesion.
        """
        if self.slice_world_z.size == 0:
            return 0
        return int(np.argmin(np.abs(self.slice_world_z - float(world_z_mm))))


def read_slice_geometry(dicom_paths: list[str]) -> SliceGeometry | None:
    """Extract per-slice world-z geometry from a DICOM series using pydicom.

    Reads ``ImagePositionPatient`` (the 3D world origin ``[X0, Y0, Z0]`` in mm),
    ``PixelSpacing`` and ``SliceThickness`` from every slice header. Returns the
    sorted z positions so a lesion's world z can be matched to a physical slice.
    Returns None when the series carries no usable position metadata (the caller
    then falls back to affine-based matching).
    """
    positions: list[float] = []
    thickness = 0.0
    for p in dicom_paths:
        try:
            ds = pydicom.dcmread(p, stop_before_pixels=True)
        except Exception:
            continue
        ipp = getattr(ds, "ImagePositionPatient", None)
        if ipp is None or len(ipp) < 3:
            continue
        positions.append(float(ipp[2]))
        if not thickness:
            st = getattr(ds, "SliceThickness", None)
            if st:
                try:
                    thickness = float(st)
                except (ValueError, TypeError):
                    pass

    if not positions:
        return None

    arr = np.asarray(positions, dtype=float)
    order = np.argsort(arr)
    z_sorted = arr[order]
    return SliceGeometry(
        slice_world_z=z_sorted,
        origin_z_mm=float(z_sorted[0]),
        slice_thickness_mm=thickness,
        n_slices=len(z_sorted),
        order=order,
    )


def geometry_from_affine(affine: np.ndarray, shape: tuple[int, ...], z_axis: int = 2) -> SliceGeometry:
    """Derive slice-z geometry from a NIfTI affine (fallback / primary for NIfTI).

    For volumes we hold as arrays (not raw DICOM), the affine already encodes the
    world transform. We sample the world z of each slice along ``z_axis`` by
    pushing the axis's voxel centres through the affine.
    """
    n = int(shape[z_axis])
    ijk = np.zeros((n, 3), dtype=float)
    ijk[:, z_axis] = np.arange(n)
    world = nib.affines.apply_affine(affine, ijk)
    z = world[:, 2]
    order = np.argsort(z)
    z_sorted = z[order]
    thickness = float(np.median(np.abs(np.diff(z_sorted)))) if n > 1 else 0.0
    return SliceGeometry(
        slice_world_z=z_sorted,
        origin_z_mm=float(z_sorted[0]),
        slice_thickness_mm=thickness,
        n_slices=n,
        order=order,
    )


def find_lesion_epicenter(suv_arr: np.ndarray, comp_mask: np.ndarray) -> tuple[int, int, int]:
    """Voxel index ``(x, y, z)`` of the absolute peak SUVmax within a lesion cluster.

    The peak-uptake voxel — not the geometric centroid — is the clinically
    meaningful epicenter for choosing a display slice, since that is where a
    reader wants to see the lesion at its hottest.
    """
    masked = np.where(comp_mask, suv_arr, -np.inf)
    flat = int(np.argmax(masked))
    return tuple(int(c) for c in np.unravel_index(flat, suv_arr.shape))  # type: ignore[return-value]


def voxel_to_world_mm(affine: np.ndarray, voxel_xyz: tuple[int, int, int]) -> np.ndarray:
    """Map a voxel index (in this volume's array order) to physical world mm.

    ``World = affine · [i, j, k, 1]``. Generalises the spec's
    ``World_Z = Origin_Z + p_z · SliceThickness`` to arbitrary orientation
    (rotations, non-axial acquisitions) rather than assuming an axis-aligned grid.
    """
    return np.asarray(nib.affines.apply_affine(affine, np.asarray(voxel_xyz, dtype=float)))


def attach_display_geometry(
    lesions: list[dict[str, Any]],
    suv_arr: np.ndarray,
    lesion_labels: np.ndarray,
    suv_affine: np.ndarray,
    ct_geometry: SliceGeometry | None,
) -> None:
    """Annotate each lesion in-place with its epicenter, world mm, and matched CT slice.

    For every lesion (identified by its label id in ``lesion_labels``):

    1. Find the peak-SUV epicenter voxel.
    2. Convert it to a physical world coordinate through the SUV affine.
    3. If CT slice geometry is available, find the CT slice whose world z is the
       closest mathematical match to the lesion's world z — the alignment that is
       robust to differing PET/CT slice counts.

    Adds keys: ``epicenter_voxel`` (x,y,z), ``world_mm`` (x,y,z),
    ``ct_slice_index`` (int|None), ``pet_slice_index`` (int, the epicenter z).
    """
    for les in lesions:
        comp_id = int(les.get("_label_id", les.get("id", 0)))
        comp_mask = lesion_labels == comp_id
        if not comp_mask.any():
            # Fall back to the centroid the labeller already stored.
            cz = int(round(les.get("centroid_voxel", [0, 0, 0])[2]))
            les["epicenter_voxel"] = les.get("centroid_voxel")
            les["world_mm"] = None
            les["pet_slice_index"] = cz
            les["ct_slice_index"] = None
            continue

        epi = find_lesion_epicenter(suv_arr, comp_mask)
        world = voxel_to_world_mm(suv_affine, epi)
        les["epicenter_voxel"] = [int(epi[0]), int(epi[1]), int(epi[2])]
        les["world_mm"] = [round(float(w), 1) for w in world]
        les["pet_slice_index"] = int(epi[2])
        les["ct_slice_index"] = (
            ct_geometry.nearest_index(float(world[2])) if ct_geometry is not None else None
        )

    logger.info(
        "display_geometry_attached",
        n_lesions=len(lesions),
        ct_slice_matching=ct_geometry is not None,
    )
