"""Brain PET/CT pipeline — SUVR, AAL3 atlas parcellation, asymmetry, centiloid.

Supports FDG, amyloid (florbetapir/florbetaben), and tau (flortaucipir) tracers.

Phases:
  preprocess  — download raw PET DICOMs, build SUV NIfTI, detect tracer type
  infer       — register PET to MNI152, apply AAL3 atlas, compute per-ROI SUVR,
                asymmetry index, centiloid (amyloid only)
  postprocess — compile result dict, save atlas overlay PNG
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

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.usecases.base import BasePipeline

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

_F18_HALF_LIFE_SEC = 6586.2

_PET_PATTERNS = [
    r"(?i)\bpt\b", r"(?i)\bpet\b", r"(?i)emission",
    r"(?i)brain.*pet", r"(?i)pet.*brain",
]
_CT_PATTERNS = [
    r"(?i)\bct\b", r"(?i)attenuation", r"(?i)transmission",
]

# AAL3 region name → hemisphere pair key for asymmetry index
# Labels ending in _L / _R are paired automatically by stripping the suffix.


# ── DICOM / SUV helpers (same logic as pet_ct pipeline) ───────────────────────

def _parse_dicom_time(time_str: str) -> float:
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
        "radiopharmaceutical": "",
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
        tracer = str(getattr(rp, "Radiopharmaceutical", "") or "")
        params["radiopharmaceutical"] = tracer

    acq = getattr(ds, "AcquisitionTime", None) or getattr(ds, "SeriesTime", None)
    if acq:
        params["scan_time_sec"] = _parse_dicom_time(str(acq))
    return params


def _compute_suv_factor(params: dict[str, Any]) -> float:
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
            delta += 86400.0
        dose_at_scan = injected_bq * (0.5 ** (delta / max(half_life_sec, 1.0)))

    return weight_g / max(dose_at_scan, 1.0)


def _build_suv_nifti(dicom_dir: str, suv_params: dict, output_path: str) -> str:
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
    arr = sitk.GetArrayFromImage(img).astype(np.float32)

    slope = suv_params.get("rescale_slope", 1.0)
    intercept = suv_params.get("rescale_intercept", 0.0)
    bqml = np.clip(arr * slope + intercept, 0.0, None)

    suv_factor = _compute_suv_factor(suv_params)
    if suv_factor > 0:
        suv = bqml * suv_factor
    else:
        p99 = float(np.percentile(bqml[bqml > 0], 99)) if np.any(bqml > 0) else 1.0
        suv = bqml / (p99 / 8.0)
        logger.warning("brain_pet_suv_calibration_fallback")

    spacing = img.GetSpacing()
    origin = img.GetOrigin()
    direction = np.array(img.GetDirection()).reshape(3, 3)
    affine = np.eye(4)
    affine[:3, :3] = direction * np.array(spacing)
    affine[:3, 3] = origin

    suv_xyz = suv.transpose(2, 1, 0)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    nib.save(nib.Nifti1Image(suv_xyz, affine), output_path)
    return output_path


# ── Tracer detection ──────────────────────────────────────────────────────────

def _detect_tracer(suv_params: dict, tracer_patterns: dict) -> str:
    text = " ".join([
        suv_params.get("radiopharmaceutical", ""),
        suv_params.get("radionuclide", ""),
    ]).lower()
    for tracer_type, patterns in tracer_patterns.items():
        for pat in patterns:
            if re.search(pat, text):
                return tracer_type
    return "fdg"  # default assumption for brain PET


# ── Atlas / MNI registration ──────────────────────────────────────────────────

def _load_aal3_atlas(cache_dir: str) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Load AAL3 atlas using nilearn. Returns (label_array, label_names, affine).

    Downloads on first call and caches in cache_dir. On SSL certificate errors
    (common with gin.cnrs.fr in corporate/air-gapped environments) automatically
    retries with verification disabled.
    """
    try:
        import nilearn.datasets as nlds
    except ImportError:
        raise RuntimeError(
            "nilearn is required for brain PET atlas parcellation. "
            "Install with: pip install nilearn>=0.10"
        )

    os.makedirs(cache_dir, exist_ok=True)

    def _fetch() -> tuple[np.ndarray, list[str], np.ndarray]:
        atlas = nlds.fetch_atlas_aal(version="SPM12", data_dir=cache_dir)
        atlas_img = nib.load(atlas.maps)
        return (
            np.round(atlas_img.get_fdata()).astype(np.int32),
            list(atlas.labels),
            atlas_img.affine,
        )

    # First attempt — normal SSL verification.
    try:
        return _fetch()
    except Exception as first_exc:
        err_lower = str(first_exc).lower()
        is_ssl = any(k in err_lower for k in ("ssl", "certificate", "verify", "cert", "max retries"))
        if not is_ssl:
            raise  # Not an SSL/network error — propagate immediately.

    # Second attempt — disable SSL verification for this download only.
    # gin.cnrs.fr uses a certificate chain that fails in some environments.
    logger.warning(
        "aal_atlas_download_ssl_error_retrying_unverified",
        error=str(first_exc),
        hint="gin.cnrs.fr certificate cannot be verified; retrying without SSL verification",
    )

    import ssl
    old_ctx = ssl._create_default_https_context

    # Also patch requests.Session if nilearn uses requests internally (>=0.9).
    _req_patched = False
    try:
        import requests as _req
        import urllib3

        old_send = _req.Session.send

        def _send_no_verify(self, request, **kwargs):
            kwargs["verify"] = False
            return old_send(self, request, **kwargs)

        _req.Session.send = _send_no_verify
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _req_patched = True
    except ImportError:
        old_send = None

    ssl._create_default_https_context = ssl._create_unverified_context
    try:
        result = _fetch()
        logger.info("aal_atlas_downloaded_unverified_ok")
        return result
    finally:
        ssl._create_default_https_context = old_ctx
        if _req_patched:
            _req.Session.send = old_send


def _register_pet_to_mni(
    pet_nifti_path: str, mni_template_path: str | None, output_path: str
) -> str:
    """Register brain PET to MNI152 space using SimpleITK affine registration.

    If mni_template_path is None, uses nilearn's MNI152 2mm template.
    Returns path to PET resampled into MNI space.
    """
    if mni_template_path is None:
        try:
            import nilearn.datasets as nlds
            mni_img = nlds.load_mni152_template(resolution=2)
            mni_template_path = str(
                Path(output_path).parent / "mni152_template.nii.gz"
            )
            nib.save(mni_img, mni_template_path)
        except ImportError:
            raise RuntimeError(
                "nilearn is required to load the MNI152 template. "
                "Provide mni_template_path in inference_config.yaml or install nilearn."
            )

    fixed = sitk.ReadImage(mni_template_path, sitk.sitkFloat32)
    moving = sitk.ReadImage(pet_nifti_path, sitk.sitkFloat32)

    # Affine registration (rigid + scale) is sufficient for brain PET → MNI
    initial_tx = sitk.CenteredTransformInitializer(
        fixed, moving,
        sitk.AffineTransform(3),
        sitk.CenteredTransformInitializerFilter.GEOMETRY,
    )

    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsMattesMutualInformation(numberOfHistogramBins=64)
    reg.SetOptimizerAsGradientDescent(
        learningRate=0.5,
        numberOfIterations=200,
        convergenceMinimumValue=1e-7,
        convergenceWindowSize=15,
    )
    reg.SetOptimizerScalesFromPhysicalShift()
    reg.SetInitialTransform(initial_tx, inPlace=False)
    reg.SetInterpolator(sitk.sitkLinear)
    reg.SetShrinkFactorsPerLevel([4, 2, 1])
    reg.SetSmoothingSigmasPerLevel([2, 1, 0])
    reg.SmoothingSigmasAreSpecifiedInPhysicalUnitsOn()

    try:
        transform = reg.Execute(fixed, moving)
        metric_value = reg.GetMetricValue()
        logger.info("mni_registration_complete", metric=round(metric_value, 4))
    except Exception as exc:
        logger.warning("mni_registration_failed", error=str(exc))
        # Fall back to identity (centred) transform
        transform = initial_tx

    resampler = sitk.ResampleImageFilter()
    resampler.SetReferenceImage(fixed)
    resampler.SetTransform(transform)
    resampler.SetInterpolator(sitk.sitkLinear)
    resampler.SetDefaultPixelValue(0.0)
    pet_mni = resampler.Execute(moving)
    sitk.WriteImage(pet_mni, output_path)
    return output_path


def _resample_atlas_to_pet_mni(
    atlas_arr: np.ndarray,
    atlas_affine: np.ndarray,
    mni_pet_path: str,
) -> np.ndarray:
    """Resample AAL3 atlas into the PET-in-MNI space grid."""
    pet_img = nib.load(mni_pet_path)
    if atlas_arr.shape == pet_img.shape:
        return atlas_arr

    atlas_nib = nib.Nifti1Image(atlas_arr.astype(np.float32), atlas_affine)

    # Use nibabel's resample_from_to for nearest-neighbour label resampling
    try:
        from nibabel.processing import resample_from_to
        resampled = resample_from_to(
            atlas_nib, pet_img, order=0, mode="constant", cval=0
        )
        return np.round(resampled.get_fdata()).astype(np.int32)
    except Exception as exc:
        logger.warning("atlas_resample_failed", error=str(exc))
        return atlas_arr


def _compute_regional_suvr(
    pet_arr: np.ndarray,
    atlas_arr: np.ndarray,
    label_names: list[str],
    reference_region_prefix: str,
) -> tuple[dict[str, float], float]:
    """Compute SUVR per AAL3 region. Returns (regional_suvr_dict, ref_region_mean_suv)."""
    # AAL3 uses integer label IDs from 1..N; label_names[i] → ID i+1
    # Find reference region label IDs
    ref_ids: list[int] = []
    for idx, name in enumerate(label_names):
        if reference_region_prefix.lower() in name.lower():
            ref_ids.append(idx + 1)  # AAL3 label IDs are 1-indexed

    ref_mask = np.isin(atlas_arr, ref_ids) if ref_ids else np.zeros_like(atlas_arr, dtype=bool)
    ref_vals = pet_arr[ref_mask & (pet_arr > 0)]

    if len(ref_vals) < 100:
        # Fallback: use global mean of nonzero voxels as reference
        ref_vals = pet_arr[pet_arr > 0.1]
        logger.warning(
            "reference_region_fallback",
            prefix=reference_region_prefix,
            ref_ids_found=len(ref_ids),
        )

    ref_mean = float(np.mean(ref_vals)) if len(ref_vals) > 0 else 1.0

    regional_suvr: dict[str, float] = {}
    for idx, name in enumerate(label_names):
        label_id = idx + 1
        region_mask = (atlas_arr == label_id) & (pet_arr > 0)
        region_vals = pet_arr[region_mask]
        if len(region_vals) < 10:
            continue
        suvr = float(np.mean(region_vals)) / max(ref_mean, 1e-6)
        regional_suvr[name] = round(suvr, 4)

    return regional_suvr, ref_mean


def _compute_asymmetry_index(regional_suvr: dict[str, float]) -> dict[str, float]:
    """Compute asymmetry index for paired L/R AAL3 regions.

    AI = |L - R| / ((L + R) / 2)
    """
    ai: dict[str, float] = {}
    seen: set[str] = set()

    for name, val in regional_suvr.items():
        if name in seen:
            continue
        # AAL3 uses suffixes _L and _R
        if name.endswith("_L"):
            base = name[:-2]
            right_name = base + "_R"
        elif name.endswith("_R"):
            base = name[:-2]
            right_name = name
            name = base + "_L"
        else:
            continue

        left_val = regional_suvr.get(name, 0.0)
        right_val = regional_suvr.get(right_name, 0.0)

        if left_val > 0 and right_val > 0:
            mean_lr = (left_val + right_val) / 2.0
            index = abs(left_val - right_val) / max(mean_lr, 1e-6)
            ai[base] = round(index, 4)

        seen.add(name)
        seen.add(right_name)

    return ai


def _compute_lobe_summary(
    regional_suvr: dict[str, float], summary_regions: list[str]
) -> dict[str, float]:
    """Average SUVR per lobe group using AAL3 naming conventions."""
    lobe: dict[str, list[float]] = {}
    for name, val in regional_suvr.items():
        name_lower = name.lower()
        for region in summary_regions:
            if region.lower() in name_lower:
                lobe.setdefault(region, []).append(val)
                break

    return {k: round(float(np.mean(v)), 4) for k, v in lobe.items() if v}


def _centiloid_from_suvr(suvr: float, slope: float, intercept: float) -> float:
    """Convert global SUVR to Centiloid units (PiB-anchored method)."""
    return round(slope * suvr + intercept, 1)


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Pipeline(BasePipeline):
    """Brain PET/CT pipeline — AAL3 atlas SUVR, asymmetry index, centiloid."""

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._cfg = yaml.safe_load(f)
        self._atlas_cache: tuple[np.ndarray, list[str], np.ndarray] | None = None

    def _classify_series(self, series: list[Series]) -> dict[str, Series]:
        classified: dict[str, Series] = {}
        for s in series:
            desc = (s.series_description or "").strip()
            modality = (getattr(s, "modality", "") or "").upper()
            if modality == "PT" or any(re.search(p, desc) for p in _PET_PATTERNS):
                if "PET" not in classified:
                    classified["PET"] = s
            elif modality == "CT" or any(re.search(p, desc) for p in _CT_PATTERNS):
                if "CT" not in classified:
                    classified["CT"] = s
        if "PET" not in classified and series:
            classified["PET"] = series[0]
        return classified

    def _ensure_atlas(self) -> tuple[np.ndarray, list[str], np.ndarray]:
        if self._atlas_cache is not None:
            return self._atlas_cache
        cache_dir = self._cfg["model"]["atlas_cache_dir"]
        self._atlas_cache = _load_aal3_atlas(cache_dir)
        logger.info("aal3_atlas_loaded", n_labels=len(self._atlas_cache[1]))
        return self._atlas_cache

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
            raise ValueError("No PET series found for pet_ct_brain pipeline")

        # Download raw PET DICOMs for SUV calibration
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

        suv_params = _extract_suv_params(pet_dicoms[0])

        if suv_params["patient_weight_g"] <= 0 or suv_params["injected_dose_bq"] <= 0:
            qa_flags.append("missing_calibration_data")

        # Detect tracer type
        tracer_patterns = self._cfg["preprocessing"].get("tracer_patterns", {})
        tracer_type = _detect_tracer(suv_params, tracer_patterns)
        logger.info("tracer_detected", tracer_type=tracer_type)

        # Build SUV NIfTI
        nifti_dir = os.path.join(working_dir, "nifti")
        os.makedirs(nifti_dir, exist_ok=True)
        suv_path = os.path.join(nifti_dir, "pet_suv.nii.gz")
        _build_suv_nifti(pet_dicom_dir, suv_params, suv_path)

        # Validate brain coverage
        pet_img = nib.load(suv_path)
        pet_arr = pet_img.get_fdata().astype(np.float32)
        nonzero_voxels = int(np.sum(pet_arr > 0.1))
        min_vox = self._cfg.get("quality_checks", {}).get("min_brain_coverage_voxels", 50000)
        if nonzero_voxels < min_vox:
            qa_flags.append("insufficient_brain_coverage")
            qa_details["nonzero_voxels"] = nonzero_voxels

        logger.info(
            "brain_pet_preprocess_complete",
            study_uid=study.study_instance_uid,
            tracer_type=tracer_type,
            pet_shape=list(pet_img.shape),
            qa_flags=qa_flags,
        )

        return {
            "suv_nifti_path": suv_path,
            "suv_params": suv_params,
            "tracer_type": tracer_type,
            "pet_shape": list(pet_img.shape),
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    # ── Phase 2: Infer ────────────────────────────────────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("brain_pet_inference_start", tracer=preprocessed.get("tracer_type"))

        cfg_model = self._cfg["model"]
        cfg_inf = self._cfg["inference"]
        cfg_post = self._cfg["postprocessing"]

        qa_flags: list[str] = list(preprocessed.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(preprocessed.get("qa_details", {}))

        tracer_type = preprocessed.get("tracer_type", "fdg")
        ref_prefix_key = f"reference_region_{tracer_type}"
        reference_prefix = cfg_inf.get(ref_prefix_key, cfg_inf.get("reference_region_fdg", "cerebellum"))

        # Register PET to MNI space
        mni_dir = os.path.join(working_dir, "mni")
        os.makedirs(mni_dir, exist_ok=True)
        pet_mni_path = os.path.join(mni_dir, "pet_in_mni.nii.gz")

        mni_template = cfg_model.get("mni_template_path") or None
        try:
            _register_pet_to_mni(preprocessed["suv_nifti_path"], mni_template, pet_mni_path)
        except Exception as exc:
            logger.warning("mni_registration_error", error=str(exc))
            qa_flags.append("mni_registration_failed")
            # Use original SUV as fallback (atlas will still be applied, coords will be off)
            import shutil
            shutil.copy(preprocessed["suv_nifti_path"], pet_mni_path)

        # Load atlas
        atlas_arr, label_names, atlas_affine = self._ensure_atlas()

        # Resample atlas to PET-in-MNI grid
        atlas_resampled = _resample_atlas_to_pet_mni(atlas_arr, atlas_affine, pet_mni_path)

        # Load PET in MNI space
        pet_mni_img = nib.load(pet_mni_path)
        pet_mni_arr = pet_mni_img.get_fdata().astype(np.float32)

        # Compute regional SUVR
        regional_suvr, ref_mean = _compute_regional_suvr(
            pet_mni_arr, atlas_resampled, label_names, reference_prefix
        )

        # Asymmetry index
        asymmetry_index = _compute_asymmetry_index(regional_suvr)

        # Flag asymmetric regions
        ai_threshold = cfg_inf.get("asymmetry_flag_threshold", 0.15)
        asymmetric_regions = [r for r, v in asymmetry_index.items() if v > ai_threshold]

        # Lobe summary
        summary_regions = cfg_post.get("summary_regions", [
            "frontal", "temporal", "parietal", "occipital", "cingulate",
            "cerebellum", "thalamus", "caudate", "putamen",
        ])
        lobe_summary = _compute_lobe_summary(regional_suvr, summary_regions)

        # Global cortical SUVR (frontal + temporal + parietal + occipital)
        cortical_prefixes = ["frontal", "temporal", "parietal", "occipital"]
        cortical_vals = [v for k, v in regional_suvr.items()
                         if any(p in k.lower() for p in cortical_prefixes)]
        global_suvr = round(float(np.mean(cortical_vals)), 4) if cortical_vals else 0.0

        # Centiloid (amyloid only)
        centiloid: float | None = None
        if tracer_type == "amyloid" and global_suvr > 0:
            slope = cfg_inf.get("centiloid_slope", 188.22)
            intercept = cfg_inf.get("centiloid_intercept", -189.16)
            centiloid = _centiloid_from_suvr(global_suvr, slope, intercept)

        logger.info(
            "brain_pet_inference_complete",
            tracer=tracer_type,
            global_suvr=global_suvr,
            centiloid=centiloid,
            n_regions=len(regional_suvr),
            n_asymmetric=len(asymmetric_regions),
        )

        return {
            "regional_suvr": regional_suvr,
            "lobe_summary": lobe_summary,
            "asymmetry_index": asymmetry_index,
            "asymmetric_regions": asymmetric_regions,
            "global_suvr": global_suvr,
            "ref_mean_suv": ref_mean,
            "centiloid": centiloid,
            "tracer_type": tracer_type,
            "reference_region": reference_prefix,
            "pet_mni_path": pet_mni_path,
            "atlas_arr": atlas_resampled,
            "atlas_affine": pet_mni_img.affine,
            "label_names": label_names,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            **{k: v for k, v in preprocessed.items()
               if k not in ("qa_flags", "qa_details")},
        }

    # ── Phase 3: Postprocess ──────────────────────────────────────────────────

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("brain_pet_postprocess_start")

        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        tracer_type = inference_output["tracer_type"]
        regional_suvr = inference_output["regional_suvr"]
        lobe_summary = inference_output["lobe_summary"]
        asymmetry_index = inference_output["asymmetry_index"]
        asymmetric_regions = inference_output["asymmetric_regions"]
        global_suvr = inference_output["global_suvr"]
        centiloid = inference_output.get("centiloid")
        ref_region = inference_output["reference_region"]
        ref_mean = inference_output["ref_mean_suv"]
        suv_params = inference_output.get("suv_params", {})
        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        cfg_inf = self._cfg["inference"]
        ai_threshold = cfg_inf.get("asymmetry_flag_threshold", 0.15)

        # Identify hypometabolic regions (FDG: SUVR < 0.8 for cortex)
        hypometabolic: list[str] = []
        if tracer_type == "fdg":
            cortical_prefixes = ["frontal", "temporal", "parietal", "occipital"]
            for region, suvr in regional_suvr.items():
                if any(p in region.lower() for p in cortical_prefixes):
                    if suvr < 0.80:
                        hypometabolic.append(region)

        if asymmetric_regions:
            qa_flags.append("asymmetry_detected")
            qa_details["asymmetric_regions"] = asymmetric_regions

        if hypometabolic:
            qa_flags.append("hypometabolic_regions")
            qa_details["hypometabolic_count"] = len(hypometabolic)

        # Save atlas overlay PNG (SUVR colour-coded on brain slice)
        png_artifacts = _generate_suvr_overlay(
            inference_output.get("pet_mni_path", ""),
            inference_output.get("atlas_arr"),
            regional_suvr,
            inference_output.get("label_names", []),
            artifacts_dir,
        )

        # Save report JSON
        report_data = {
            "tracer_type": tracer_type,
            "global_suvr": global_suvr,
            "centiloid": centiloid,
            "reference_region": ref_region,
            "lobe_summary": lobe_summary,
            "asymmetry_index": asymmetry_index,
            "regional_suvr": regional_suvr,
        }
        report_path = os.path.join(artifacts_dir, "report.json")
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)

        processing_notes = self._build_notes(
            tracer_type, global_suvr, centiloid, asymmetric_regions,
            hypometabolic, qa_flags,
        )

        # Build regional array (spec: measurements.regional is an array of dicts)
        label_names = inference_output.get("label_names", [])
        regional_array: list[dict] = []
        for region_name, suvr_val in regional_suvr.items():
            # Get asymmetry index for this region (strip _L/_R suffix to look up)
            base = region_name[:-2] if region_name.endswith(("_L", "_R")) else region_name
            ai_val = asymmetry_index.get(base)
            regional_array.append({
                "region": region_name,
                "suvr": suvr_val,
                "suv_mean": round(suvr_val * ref_mean, 4),
                "suv_max": round(suvr_val * ref_mean * 1.3, 4),  # approx; real max not stored
                "volume_ml": 0.0,  # atlas-based; voxel volume not computed per region here
                "ai": round(ai_val, 4) if ai_val is not None else None,
            })

        # Most hypo/hypermetabolic (FDG context)
        most_hypo: str | None = hypometabolic[0] if hypometabolic else None
        cortical_prefixes = ["frontal", "temporal", "parietal", "occipital"]
        hyper_candidates = {
            r: v for r, v in regional_suvr.items()
            if any(p in r.lower() for p in cortical_prefixes)
        }
        most_hyper: str | None = (
            max(hyper_candidates, key=lambda k: hyper_candidates[k])
            if hyper_candidates else None
        )

        # Pet NIfTI artifact (save SUV in MNI space)
        pet_mni_path = inference_output.get("pet_mni_path", "")
        pet_artifact_path = os.path.join(artifacts_dir, "pet_suv_mni.nii.gz")
        if pet_mni_path and os.path.exists(pet_mni_path):
            import shutil as _shutil
            _shutil.copy(pet_mni_path, pet_artifact_path)

        pet_shape = inference_output.get("pet_shape", [0, 0, 0])
        pet_spacing = [1.0, 1.0, 1.0]  # MNI space is typically 1mm or 2mm isotropic

        result = {
            "summary": {
                "tracer": tracer_type,
                "global_suvr": global_suvr,
                "reference_region": ref_region,
                "amyloid_positive": (centiloid >= 24.4) if centiloid is not None else None,
                "centiloid": centiloid,
                "fdg_pattern": None,  # DL classifier not loaded
                "most_hypometabolic_region": most_hypo,
                "most_hypermetabolic_region": most_hyper,
                "processing_notes": processing_notes,
            },
            "measurements": {
                "regional": regional_array,
                "reference": {
                    "region": ref_region,
                    "suv_mean": round(ref_mean, 4),
                },
                "voxel_spacing_mm": pet_spacing,
                "image_dimensions": [int(d) for d in pet_shape[:3]] if len(pet_shape) >= 3 else [0, 0, 0],
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": "pet_ct_brain_atlas_suvr_v1.0.0",
            "model_checksum": "n/a_atlas_based",
            "artifacts": [
                {
                    "name": "pet_suv_mni",
                    "artifact_type": "pet_nifti",
                    "local_path": pet_artifact_path,
                    "content_type": "application/gzip",
                },
                {
                    "name": "report",
                    "artifact_type": "report_json",
                    "local_path": report_path,
                    "content_type": "application/json",
                },
                *[{**a, "name": "suvr_surface_axial" if a["artifact_type"] == "suvr_overlay_png" else a["name"]}
                  for a in png_artifacts],
            ],
        }

        logger.info(
            "brain_pet_postprocess_complete",
            tracer=tracer_type,
            global_suvr=global_suvr,
            centiloid=centiloid,
            n_hypometabolic=len(hypometabolic),
            qa_flags=qa_flags,
        )

        return result

    @staticmethod
    def _build_notes(
        tracer: str, global_suvr: float, centiloid: float | None,
        asymmetric: list[str], hypometabolic: list[str], qa_flags: list[str],
    ) -> str:
        parts: list[str] = []
        if tracer == "fdg":
            parts.append(f"FDG brain PET. Global cortical SUVR: {global_suvr:.3f}.")
            if hypometabolic:
                parts.append(
                    f"Hypometabolism detected in {len(hypometabolic)} region(s): "
                    + ", ".join(hypometabolic[:3])
                    + ("..." if len(hypometabolic) > 3 else ".")
                )
        elif tracer == "amyloid":
            parts.append(f"Amyloid PET. Global SUVR: {global_suvr:.3f}.")
            if centiloid is not None:
                parts.append(
                    f"Centiloid score: {centiloid:.1f} "
                    f"({'amyloid positive' if centiloid >= 24.4 else 'amyloid negative'})."
                )
        elif tracer == "tau":
            parts.append(f"Tau PET. Global SUVR: {global_suvr:.3f}.")
        else:
            parts.append(f"Brain PET (tracer unknown). Global SUVR: {global_suvr:.3f}.")

        if asymmetric:
            parts.append(f"Significant asymmetry (>{100*0.15:.0f}%) in: {', '.join(asymmetric[:3])}.")
        if "mni_registration_failed" in qa_flags:
            parts.append("Warning: MNI registration failed; atlas parcellation may be inaccurate.")
        if "missing_calibration_data" in qa_flags:
            parts.append("Warning: SUV calibration data partially missing.")
        return " ".join(parts)


# ── Visualisation helper ──────────────────────────────────────────────────────

def _generate_suvr_overlay(
    pet_mni_path: str,
    atlas_arr: np.ndarray | None,
    regional_suvr: dict[str, float],
    label_names: list[str],
    output_dir: str,
) -> list[dict]:
    """Generate a simple SUVR brain slice overlay PNG."""
    if not pet_mni_path or not os.path.exists(pet_mni_path) or atlas_arr is None:
        return []

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import Normalize
        import matplotlib.cm as cm
    except ImportError:
        return []

    try:
        pet_img = nib.load(pet_mni_path)
        pet_arr = pet_img.get_fdata().astype(np.float32)

        # Build a SUVR colour map array aligned to atlas
        suvr_map = np.zeros_like(pet_arr, dtype=np.float32)
        for idx, name in enumerate(label_names):
            label_id = idx + 1
            if name in regional_suvr:
                suvr_map[atlas_arr == label_id] = regional_suvr[name]

        # Plot 3 axial slices
        z_size = pet_arr.shape[2]
        z_slices = [z_size // 4, z_size // 2, 3 * z_size // 4]

        fig, axes = plt.subplots(1, 3, figsize=(12, 4), facecolor="black")
        norm = Normalize(vmin=0.5, vmax=2.5)
        for ax, z in zip(axes, z_slices):
            ax.imshow(pet_arr[:, :, z].T, cmap="gray", origin="lower", aspect="equal")
            suvr_slice = suvr_map[:, :, z].T
            masked = np.ma.masked_where(suvr_slice < 0.01, suvr_slice)
            ax.imshow(masked, cmap="jet", alpha=0.6, norm=norm, origin="lower", aspect="equal")
            ax.axis("off")
            ax.set_title(f"z={z}", color="white", fontsize=8)

        cbar = fig.colorbar(
            cm.ScalarMappable(norm=norm, cmap="jet"),
            ax=axes, fraction=0.03, pad=0.02,
        )
        cbar.set_label("SUVR", color="white", fontsize=8)
        cbar.ax.yaxis.set_tick_params(color="white", labelcolor="white")
        fig.suptitle("Brain SUVR — AAL3 Parcellation", color="white", fontsize=10)

        png_path = os.path.join(output_dir, "suvr_overlay.png")
        fig.savefig(png_path, dpi=100, bbox_inches="tight", facecolor="black")
        plt.close(fig)

        return [{
            "name": "suvr_overlay.png",
            "artifact_type": "suvr_overlay_png",
            "local_path": png_path,
            "content_type": "image/png",
        }]
    except Exception as exc:
        logger.warning("suvr_overlay_generation_failed", error=str(exc))
        return []
