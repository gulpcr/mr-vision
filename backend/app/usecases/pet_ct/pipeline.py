"""Whole-body FDG-PET/CT oncology pipeline — orchestrator / facade.

This module is deliberately thin: it wires together the four domain stages and
satisfies the :class:`BasePipeline` three-phase contract
(``preprocess`` / ``infer`` / ``postprocess``). All the heavy logic lives in the
stage modules so each concern is testable in isolation:

    registration.py   Stage 2 — DICOM/SUV geometry, CT co-registration, and the
                       world-coordinate per-lesion slice matching.
    thresholding.py    Stage 1 — reference SUV stats, detection threshold,
                       TotalSegmentator organ masks, connected-component
                       clustering, and per-lesion measurement + naming.
    visualization.py   Stage 3 — MIP/fused renderers plus the MedGemma-oriented
                       numbered roadmap and composite regional crops.
    prompts.py         Stage 4 — the structured MedGemma data-matrix payload.

The clinical numbers and the Celery task lifecycle are unchanged from the prior
monolith — this is a structural refactor, not a behavioural one — except for two
additive capabilities: world-coordinate display-slice matching (attached to each
lesion) and an optional local MedGemma narrative merged into the summary.
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
from app.usecases.pet_ct import registration, thresholding, visualization

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

_PET_PATTERNS = [
    r"(?i)\bpt\b", r"(?i)\bpet\b", r"(?i)emission",
    r"(?i)\bfdg\b", r"(?i)wholebody", r"(?i)whole.*body",
]
_CT_PATTERNS = [
    r"(?i)\bct\b", r"(?i)attenuation", r"(?i)transmission",
    r"(?i)low.*dose", r"(?i)ct.*corr",
]
_CT_SCOUT_PATTERNS = [
    r"(?i)topogram", r"(?i)scout", r"(?i)localizer", r"(?i)localiser",
    r"(?i)surview", r"(?i)scanogram", r"(?i)\bscano\b",
]


class Pipeline(BasePipeline):
    """Whole-body FDG-PET/CT oncology pipeline (SwinUNETR DL or PERCIST threshold)."""

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
                    weights=weights_path, error=str(exc),
                )

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
            device=str(self._device), in_channels=in_channels,
            out_channels=out_channels, feature_size=feature_size,
            checksum=self._model_checksum,
        )

    def _run_dl_inference(self, suv_arr: np.ndarray, ct_arr: np.ndarray | None) -> np.ndarray:
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

        clip_suv = float(pre_cfg.get("clip_suv_max", 20.0))
        suv_norm = (np.clip(suv_arr, 0.0, clip_suv) / clip_suv).astype(np.float32)

        if in_channels >= 2 and ct_arr is not None:
            clip_ct_min = float(pre_cfg.get("clip_ct_min", -1000.0))
            clip_ct_max = float(pre_cfg.get("clip_ct_max", 1000.0))
            ct_norm = (
                (np.clip(ct_arr, clip_ct_min, clip_ct_max) - clip_ct_min)
                / (clip_ct_max - clip_ct_min)
            ).astype(np.float32)
            vol = np.stack([suv_norm, ct_norm], axis=0)
        else:
            channels = [suv_norm] + [np.zeros_like(suv_norm)] * (in_channels - 1)
            vol = np.stack(channels, axis=0)

        input_tensor = torch.from_numpy(vol[np.newaxis]).to(self._device)

        with torch.no_grad():
            output = sliding_window_inference(
                inputs=input_tensor, roi_size=roi_size, sw_batch_size=sw_batch_size,
                predictor=self._model, overlap=overlap, mode=mode,
            )

        if out_channels >= 2:
            probs = F.softmax(output, dim=1)
            lesion_prob = probs[0, 1].cpu().numpy()
        else:
            lesion_prob = torch.sigmoid(output[0, 0]).cpu().numpy()

        return (lesion_prob >= dl_threshold).astype(np.int32)

    # ── Series classification ─────────────────────────────────────────────────

    def _classify_series(self, series: list[Series]) -> dict[str, Series]:
        """Pick the PET and CT series to fuse (skip single-plane CT scouts)."""
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
            return any(re.search(p, desc) for p in _CT_SCOUT_PATTERNS) or (0 < n <= 2)

        classified: dict[str, Series] = {}
        if pet_candidates:
            classified["PET"] = max(pet_candidates, key=_instances)
        if ct_candidates:
            diagnostic = [s for s in ct_candidates if not _is_scout(s)]
            pool = diagnostic or ct_candidates
            classified["CT"] = max(pool, key=_instances)
            if not diagnostic:
                logger.warning(
                    "ct_series_only_scouts",
                    chosen=classified["CT"].series_description,
                    instances=_instances(classified["CT"]),
                )
        if "PET" not in classified and series:
            classified["PET"] = series[0]
        return classified

    # ── Phase 1: Preprocess ───────────────────────────────────────────────────

    def preprocess(
        self, study: Study, series: list[Series], working_dir: str,
        pacs: PACSClient, event_loop: Any = None,
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

        pet_dicom_dir = os.path.join(working_dir, "pet_dicoms")
        os.makedirs(pet_dicom_dir, exist_ok=True)
        pet_dicoms: list[str] = loop.run_until_complete(
            pacs.download_series_dicoms(
                study.study_instance_uid, classified["PET"].series_instance_uid, pet_dicom_dir,
            )
        )
        if not pet_dicoms:
            raise ValueError("Failed to download PET DICOM files")

        suv_params = registration.extract_suv_params(pet_dicoms[0])

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

        nifti_dir = os.path.join(working_dir, "nifti")
        os.makedirs(nifti_dir, exist_ok=True)
        suv_nifti_path = os.path.join(nifti_dir, "pet_suv.nii.gz")
        raw_nifti_path = os.path.join(nifti_dir, "pet_raw.nii.gz")
        registration.build_suv_nifti(
            pet_dicom_dir, suv_params, suv_nifti_path, raw_output_path=raw_nifti_path
        )

        pet_img = nib.load(suv_nifti_path)
        pet_dims = pet_img.shape
        min_slices = self._cfg.get("quality_checks", {}).get("min_slices", 100)
        if max(pet_dims) < min_slices:
            qa_flags.append("insufficient_coverage")
            qa_details["pet_dimensions"] = list(pet_dims)

        ct_nifti_path = None
        ct_original_path = None  # full-resolution CT (pre-resample) for accurate HU
        if "CT" in classified:
            ct_nifti_path = os.path.join(nifti_dir, "ct.nii.gz")
            try:
                loop.run_until_complete(
                    pacs.download_series_as_nifti(
                        study.study_instance_uid, classified["CT"].series_instance_uid, ct_nifti_path,
                    )
                )
                ct_original_path = ct_nifti_path
                ct_pet_path = os.path.join(nifti_dir, "ct_in_pet_space.nii.gz")
                registration.resample_ct_to_pet(suv_nifti_path, ct_nifti_path, ct_pet_path)
                ct_nifti_path = ct_pet_path
            except Exception as exc:
                logger.warning("ct_download_or_resample_failed", error=str(exc))
                ct_nifti_path = None
                ct_original_path = None
                qa_flags.append("ct_registration_failed")

        logger.info(
            "pet_ct_preprocess_complete",
            study_uid=study.study_instance_uid, pet_dims=list(pet_dims),
            has_ct=ct_nifti_path is not None,
            suv_calibrated=registration.compute_suv_factor(suv_params) > 0,
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

        # Original full-resolution CT (accurate per-lesion HU + CT slice geometry).
        orig_ct_img = None
        orig_ct_path = preprocessed.get("ct_original_nifti_path")
        if orig_ct_path and os.path.exists(orig_ct_path):
            try:
                orig_ct_img = nib.load(orig_ct_path)
            except Exception as exc:
                logger.warning("original_ct_load_failed", error=str(exc))
                orig_ct_img = None

        # ── Reference region extraction (single shared TotalSegmentator pass) ──
        ct_arr = None
        ref_stats: dict[str, dict[str, float]] = {}
        structure_labels = None
        structure_names: dict[int, str] = {}
        seg_ct_path = orig_ct_path or preprocessed.get("ct_nifti_path")
        if preprocessed.get("ct_nifti_path"):
            ct_img = nib.load(preprocessed["ct_nifti_path"])
            ct_arr = ct_img.get_fdata().astype(np.float32)
            if ct_arr.shape == suv_arr.shape:
                struct_cfg = cfg_inf.get("structure_labeling", {})
                if struct_cfg.get("enabled", True) and seg_ct_path:
                    try:
                        res = thresholding.segment_structures_multilabel(
                            seg_ct_path, suv_arr.shape, working_dir,
                            preprocessed["suv_nifti_path"], struct_cfg,
                        )
                        if res is not None:
                            structure_labels, structure_names = res
                    except Exception as exc:
                        logger.warning("structure_labeling_failed", error=str(exc))

                use_seg = cfg_inf.get("reference_organ_segmentation", True)
                if use_seg:
                    if structure_labels is not None:
                        ref_masks = thresholding.masks_from_labels(
                            structure_labels, structure_names, ("liver", "aorta")
                        )
                    elif seg_ct_path:
                        ref_masks = thresholding.segment_reference_organs(
                            seg_ct_path, suv_arr.shape, working_dir, preprocessed["suv_nifti_path"],
                        )
                    else:
                        ref_masks = {}
                    liver_stats = thresholding.suv_stats_from_mask(suv_arr, ref_masks.get("liver"))
                    aorta_stats = thresholding.suv_stats_from_mask(suv_arr, ref_masks.get("aorta"))
                    if liver_stats:
                        liver_stats["source"] = "totalseg_liver"
                        ref_stats["liver"] = liver_stats
                    if aorta_stats:
                        aorta_stats["source"] = "totalseg_aorta"
                        ref_stats["mediastinum"] = aorta_stats
                if "liver" not in ref_stats or "mediastinum" not in ref_stats:
                    heur = thresholding.extract_reference_region_stats(suv_arr, ct_arr, cfg_post)
                    ref_stats.setdefault("liver", heur["liver"])
                    ref_stats.setdefault("mediastinum", heur["mediastinum"])
            else:
                logger.warning(
                    "ct_pet_shape_mismatch", ct_shape=ct_arr.shape, pet_shape=suv_arr.shape,
                )

        if not ref_stats:
            valid = suv_arr[suv_arr > 0.5]
            if len(valid) > 0:
                ref_stats["liver"] = {
                    "mean": float(np.percentile(valid, 65)),
                    "std": float(np.std(valid) * 0.25),
                    "n_voxels": 0, "fallback": True,
                }
            else:
                ref_stats["liver"] = {"mean": 2.5, "std": 0.6, "n_voxels": 0, "fallback": True}
            ref_stats["mediastinum"] = {
                "mean": ref_stats["liver"]["mean"] * 0.5, "std": 0.2,
                "n_voxels": 0, "fallback": True,
            }

        liver_mean = ref_stats["liver"]["mean"]
        liver_std = ref_stats["liver"]["std"]
        med_mean = ref_stats["mediastinum"]["mean"]

        # ── Detection threshold (config-driven; unchanged behaviour) ───────────
        suv_params = preprocessed.get("suv_params", {})
        suv_calibrated = registration.compute_suv_factor(suv_params) > 0
        threshold, thr_details = thresholding.compute_detection_threshold(
            cfg_inf, liver_mean, liver_std, suv_calibrated
        )

        # ── Lesion detection: SwinUNETR → PERCIST threshold fallback ───────────
        inference_method: str | None = None
        raw_mask = None
        if self._model is not None:
            try:
                logger.info(
                    "dl_lesion_inference_start",
                    model_version=self._model_version, device=str(self._device),
                )
                raw_mask = self._run_dl_inference(suv_arr, ct_arr)
                inference_method = "swin_unetr"
                logger.info("dl_lesion_inference_complete", raw_lesion_voxels=int(raw_mask.sum()))
            except Exception as exc:
                logger.warning("dl_inference_failed_falling_back_to_threshold", error=str(exc))
                raw_mask = (suv_arr >= threshold).astype(np.int32)
                inference_method = "threshold_fallback"
        else:
            logger.info(
                "suv_threshold",
                liver_mean=round(liver_mean, 3), liver_std=round(liver_std, 3),
                percist_reference=thr_details.get("percist_threshold"),
                effective_threshold=round(threshold, 3), source=thr_details.get("source"),
            )
            raw_mask = (suv_arr >= threshold).astype(np.int32)
            inference_method = "threshold"

        # ── Physiologic-uptake suppression (threshold detection only) ──────────
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
                organs = list(supp_cfg.get("exclude_organs", []) or [])
                if structure_labels is not None and organs:
                    masks = thresholding.masks_from_labels(structure_labels, structure_names, organs)
                    if masks:
                        excl_mask = np.zeros(suv_arr.shape, dtype=bool)
                        for m in masks.values():
                            excl_mask |= m
                        dilate = int(supp_cfg.get("dilate_voxels", 0))
                        if dilate > 0:
                            excl_mask = ndimage.binary_dilation(excl_mask, iterations=dilate)
                        excluded_organs = list(masks.keys())
                        suppression_method = "totalsegmentator"
                elif seg_ct_path:
                    result = thresholding.run_physiologic_organ_exclusion(
                        seg_ct_path, suv_arr.shape, working_dir, supp_cfg,
                        preprocessed["suv_nifti_path"],
                    )
                    if result is not None:
                        excl_mask, excluded_organs = result
                        suppression_method = "totalsegmentator"
            except Exception as exc:
                logger.warning("physiologic_organ_exclusion_failed", error=str(exc))
                excl_mask = None
        if excl_mask is None:
            excl_mask = thresholding.build_physiological_exclusion_mask(suv_arr.shape, cfg_post)
        # Always-on geometric brain backstop (physiologic cerebral uptake is the most
        # intense in the body; a coarse --fast brain mask can leak at its margins).
        if supp_cfg.get("brain_geometric_backstop", True) and suv_arr.ndim == 3:
            brain_frac = float(cfg_post.get("exclude_brain_top_frac", 0.12))
            brain_z = int(suv_arr.shape[2] * (1 - brain_frac))
            excl_mask[:, :, brain_z:] = True
        raw_mask[excl_mask] = 0

        # ── Stage 1: cluster + measure + name ──────────────────────────────────
        lesions, labeled, rejected = thresholding.label_and_measure_lesions(
            detection_mask=raw_mask,
            suv_arr=suv_arr,
            ct_arr=ct_arr,
            orig_ct_img=orig_ct_img,
            affine=affine,
            voxel_spacing=voxel_spacing,
            voxel_vol_ml=voxel_vol_ml,
            structure_labels=structure_labels,
            structure_names=structure_names,
            liver_mean=liver_mean,
            cfg_inf=cfg_inf,
        )
        n_rejected_oversize = rejected["oversize"]
        n_rejected_concordance = rejected["concordance"]

        # ── Stage 2: world-coordinate display-slice matching per lesion ────────
        ct_geometry = None
        if orig_ct_img is not None:
            try:
                ct_geometry = registration.geometry_from_affine(
                    orig_ct_img.affine, orig_ct_img.shape, z_axis=2
                )
            except Exception as exc:
                logger.warning("ct_slice_geometry_failed", error=str(exc))
        registration.attach_display_geometry(lesions, suv_arr, labeled, affine, ct_geometry)
        for les in lesions:
            les.pop("_label_id", None)  # strip the internal component id before persisting

        logger.info(
            "pet_ct_inference_complete",
            inference_method=inference_method, suppression_method=suppression_method,
            excluded_organs=excluded_organs, rejected_non_concordant=n_rejected_concordance,
            rejected_oversize=n_rejected_oversize, suv_calibrated=suv_calibrated,
            threshold=round(threshold, 2), lesion_count=len(lesions),
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

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
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

        global_suv_max = max((x["suv_max"] for x in lesions), default=0.0)
        deauville = thresholding.deauville_score(global_suv_max, med_mean, liver_mean)
        tumor_to_liver_ratio = (
            round(global_suv_max / liver_mean, 2) if liver_mean > 0 and lesions else None
        )

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
                "CT unavailable — physiologic-uptake suppression and CT concordance "
                "filtering were disabled"
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

        percist_score = "No Active Disease" if not lesions else "Active Disease (Baseline)"
        radiopharmaceutical = suv_params.get("tracer_name", suv_params.get("radionuclide", "FDG"))

        max_suv_limit = self._cfg.get("quality_checks", {}).get("max_expected_suv", 50.0)
        if global_suv_max > max_suv_limit:
            qa_flags.append("suv_range_suspicious")
            qa_details["suv_max_observed"] = round(global_suv_max, 1)

        # NIfTI + report JSON artifacts
        suv_artifact_path = os.path.join(artifacts_dir, "pet_suv.nii.gz")
        nib.save(nib.Nifti1Image(suv_arr, affine), suv_artifact_path)
        seg_path = os.path.join(artifacts_dir, "lesion_mask.nii.gz")
        nib.save(nib.Nifti1Image(lesion_mask, affine), seg_path)

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

        # ── Stage 3: visualization artifacts ───────────────────────────────────
        colormap = self._cfg.get("postprocessing", {}).get("mip_colormap", "hot")
        mip_artifacts: list[dict] = []
        if self._cfg.get("postprocessing", {}).get("generate_mip", True):
            mip_artifacts = visualization.generate_mip_pngs(suv_arr, artifacts_dir, colormap)

        fused_artifacts: list[dict] = []
        if self._cfg.get("postprocessing", {}).get("generate_fused", True):
            fused_artifacts = visualization.generate_fused_petct_pngs(
                suv_arr, ct_arr, artifacts_dir, colormap
            )

        # MedGemma-oriented composites: numbered coronal roadmap + regional crops.
        roadmap_artifact: dict | None = None
        composite_artifacts: list[dict] = []
        want_composites = (
            self._cfg.get("postprocessing", {}).get("generate_composite", True) and bool(lesions)
        )
        if want_composites:
            roadmap_artifact = visualization.generate_lesion_mip_roadmap(
                suv_arr, lesions, artifacts_dir, colormap
            )
            composite_artifacts = visualization.generate_composite_crops(
                suv_arr, ct_arr, lesion_mask, lesions, artifacts_dir, voxel_spacing, colormap
            )

        # CT + raw-PET NIfTI passthrough artifacts (display rendering).
        ct_nifti_path = inference_output.get("ct_nifti_path")
        ct_artifacts = []
        if ct_nifti_path and os.path.exists(ct_nifti_path):
            import shutil
            ct_artifact_path = os.path.join(artifacts_dir, "ct.nii.gz")
            shutil.copy2(ct_nifti_path, ct_artifact_path)
            ct_artifacts = [{
                "name": "ct", "artifact_type": "ct_nifti",
                "local_path": ct_artifact_path, "content_type": "application/gzip",
            }]

        raw_nifti_path = inference_output.get("raw_nifti_path")
        raw_pet_artifacts = []
        if raw_nifti_path and os.path.exists(raw_nifti_path):
            import shutil
            raw_artifact_path = os.path.join(artifacts_dir, "pet_raw.nii.gz")
            shutil.copy2(raw_nifti_path, raw_artifact_path)
            raw_pet_artifacts = [{
                "name": "pet_raw", "artifact_type": "pet_nifti",
                "local_path": raw_artifact_path, "content_type": "application/gzip",
            }]

        inference_method = inference_output.get("inference_method", "threshold")
        suppression_method = inference_output.get("suppression_method", "geometric")
        excluded_organs = inference_output.get("excluded_organs", []) or []
        rejected_non_concordant = int(inference_output.get("rejected_non_concordant", 0) or 0)

        # Tumor-positive call (keyed off liver SUVmean; config-driven).
        cfg_inf = self._cfg.get("inference", {})
        if cfg_inf.get("calibrated_threshold_source", "liver_mean") == "liver_mean" and liver_mean > 0:
            diag_factor = float(cfg_inf.get("diagnosis_liver_factor", 1.0))
            diag_cutoff = diag_factor * liver_mean
            diag_label = "liver SUVmean" if diag_factor == 1.0 else f"liver SUVmean × {diag_factor:g}"
        else:
            diag_cutoff = float(cfg_inf.get("suv_threshold_absolute", 2.5))
            diag_label = "SUV"
        diagnosis = thresholding.derive_diagnosis(lesions, deauville, diag_cutoff, diag_label)
        if not suv_calibrated:
            diagnosis = (
                "NON-QUANTITATIVE (uncalibrated SUV) — interpret uptake relatively, "
                "not by absolute SUV. " + diagnosis
            )
        processing_notes = self._build_notes(
            lesions, qa_flags, percist_threshold, deauville, inference_method,
            suppression_method=suppression_method, excluded_organs=excluded_organs,
            rejected_non_concordant=rejected_non_concordant, suv_calibrated=suv_calibrated,
            rejected_oversize=n_rejected_oversize,
        )

        summary = {
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
        }

        # NB: the AI-authored narrative (Gemini OR local MedGemma) is generated by
        # the Celery task hook (infrastructure/queue/tasks.py) AFTER postprocess, so
        # it can read the rendered images from disk and store the structured
        # summary["ai_report"] the UI/PDF consume. It is intentionally NOT called
        # here — see _maybe_add_medgemma_findings' removal.

        result = {
            "summary": summary,
            "measurements": {
                "lesions": lesions,
                "reference_organs": {
                    "liver_suv_mean": round(liver_mean, 3),
                    "liver_suv_sd": round(liver_std, 3),
                    "mediastinum_suv_mean": round(med_mean, 3),
                },
                "whole_body": {
                    "mtv_total_ml": total_mtv, "tlg_total": total_tlg,
                    "suvmax_body": round(global_suv_max, 2), "lesion_count": len(lesions),
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
                    "name": "pet_suv", "artifact_type": "pet_nifti",
                    "local_path": suv_artifact_path, "content_type": "application/gzip",
                },
                {
                    "name": "lesion_mask", "artifact_type": "segmentation_nifti",
                    "local_path": seg_path, "content_type": "application/gzip",
                },
                {
                    "name": "report", "artifact_type": "report_json",
                    "local_path": report_path, "content_type": "application/json",
                },
                *mip_artifacts,
                *fused_artifacts,
                *([roadmap_artifact] if roadmap_artifact else []),
                *composite_artifacts,
                *ct_artifacts,
                *raw_pet_artifacts,
            ],
        }

        logger.info(
            "pet_ct_postprocess_complete",
            lesion_count=len(lesions), total_mtv=total_mtv, deauville=deauville,
            percist=percist_score, qa_flags=qa_flags,
        )
        return result

    @staticmethod
    def _build_notes(
        lesions: list[dict], qa_flags: list[str], threshold: float, deauville: int,
        inference_method: str = "threshold", suppression_method: str = "geometric",
        excluded_organs: list[str] | None = None, rejected_non_concordant: int = 0,
        suv_calibrated: bool = True, rejected_oversize: int = 0,
    ) -> str:
        parts: list[str] = []

        if not suv_calibrated:
            parts.append(
                "NON-QUANTITATIVE: SUV calibration unavailable — a relative "
                "(liver-referenced) threshold was used and absolute SUV values "
                "are not reliable."
            )

        method_label = {
            "swin_unetr": "SwinUNETR deep-learning segmentation",
            "threshold_fallback": "PERCIST SUV-threshold (DL fallback)",
            "threshold": "PERCIST SUV-threshold",
        }.get(inference_method, inference_method)
        parts.append(f"Detection method: {method_label}.")

        if rejected_oversize > 0:
            parts.append(
                f"Rejected {rejected_oversize} oversized focus/foci exceeding the "
                "single-lesion volume bound (diffuse uptake, not a discrete lesion)."
            )

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
