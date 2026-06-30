from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
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

# Abdomen MRI sequence classification patterns.
SEQUENCE_PATTERNS = {
    "T2_HASTE": [
        r"(?i)haste",
        r"(?i)t2.*haste",
        r"(?i)haste.*t2",
        r"(?i)t2.*ss.*fse",
        r"(?i)ssfse",
        r"(?i)t2.*cor",
        r"(?i)t2.*tse.*cor",
        r"(?i)mrcp",
    ],
    "T1_IP": [
        r"(?i)t1.*ip",
        r"(?i)t1.*in.*phase",
        r"(?i)in.*phase.*t1",
        r"(?i)t1.*dual",
        r"(?i)dual.*t1",
        r"(?i)t1w.*ip",
    ],
    "T1_OP": [
        r"(?i)t1.*op",
        r"(?i)t1.*out.*phase",
        r"(?i)out.*phase.*t1",
        r"(?i)t1w.*op",
    ],
    "DWI": [
        r"(?i)dwi",
        r"(?i)diffusion",
        r"(?i)dw.*mri",
        r"(?i)\badc\b",
        r"(?i)apparent.*diffusion",
    ],
    "DCE": [
        r"(?i)dce",
        r"(?i)dynamic",
        r"(?i)t1.*vibe",
        r"(?i)vibe",
        r"(?i)t1.*lava",
        r"(?i)lava",
        r"(?i)t1.*fame",
        r"(?i)t1.*liver",
        r"(?i)arterial",
        r"(?i)portal",
        r"(?i)delayed",
    ],
}


class Pipeline(BasePipeline):
    """Abdomen MRI segmentation pipeline.

    Segments five abdominal organs: liver, spleen, right kidney, left kidney,
    and pancreas.  When no trained model weights are available the pipeline
    falls back to synthetic inference (intensity-thresholding + connected
    components) so that the full three-phase contract can be exercised without
    GPU resources or downloaded bundles.

    Performs:
    - Sequence classification from DICOM series descriptions
    - NIfTI download and preprocessing (T2 HASTE primary + T1 IP/OP)
    - QA checks for spacing, coverage, and motion artifacts
    - Segmentation (real model or synthetic fallback)
    - Per-organ volumetrics, total parenchymal volume
    - Hepatomegaly and splenomegaly detection vs reference ranges
    - Artifact generation: segmentation.nii.gz, report.json, optional previews
    """

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._config = yaml.safe_load(f)
        self._model = None
        self._device = None
        self._model_checksum_cache: str | None = None
        self._sw_model = None
        self._sw_device = None
        sw_cfg = self._config.get("swin_unetr", {})
        sw_path = sw_cfg.get("custom_weights_path")
        if sw_path:
            try:
                self._load_swin_unetr(sw_path, sw_cfg)
            except Exception as exc:
                logger.warning("swin_unetr_load_failed", weights=sw_path, error=str(exc))

        # Optional dedicated lesion-detection model (mirrors chest_mri Problem C).
        # Inert when its custom_weights_path is null — see inference_config.yaml.
        self._lesion_model, self._lesion_device = self._load_aux_model(
            self._config.get("lesion_detection", {})
        )

    def _load_aux_model(self, cfg: dict):
        """Load an auxiliary single-channel SwinUNETR model from cfg.

        Returns ``(model, device)`` or ``(None, None)`` when no weights are
        configured or loading fails. Used for the optional dedicated
        lesion-detection model; non-intrusive and leaves the primary organ
        segmentation intact when absent.
        """
        path = cfg.get("custom_weights_path")
        if not path:
            return None, None
        try:
            import torch
            from monai.networks.nets import SwinUNETR

            device_str = self._config["inference"].get("device", "cuda")
            if device_str == "auto":
                device_str = "cuda" if torch.cuda.is_available() else "cpu"
            device = torch.device(device_str)
            model = SwinUNETR(
                img_size=tuple(cfg.get("roi_size", [96, 96, 96])),
                in_channels=cfg.get("in_channels", 1),
                out_channels=cfg.get("out_channels", 2),
                feature_size=cfg.get("feature_size", 48),
                use_checkpoint=cfg.get("use_checkpoint", False),
            )
            state = torch.load(path, map_location="cpu", weights_only=True)
            if isinstance(state, dict) and "state_dict" in state:
                state = state["state_dict"]
            elif isinstance(state, dict) and "model" in state:
                state = state["model"]
            model.load_state_dict(state, strict=False)
            model.to(device)
            model.eval()
            logger.info("aux_model_loaded", path=path, out_channels=cfg.get("out_channels", 2))
            return model, device
        except Exception as exc:
            logger.warning("aux_model_load_failed", path=path, error=str(exc))
            return None, None

    def _run_aux_model(self, model, device, img_data: np.ndarray, cfg: dict) -> np.ndarray:
        """Run a loaded auxiliary SwinUNETR → argmax label array (uint8)."""
        import torch
        from monai.inferers import sliding_window_inference

        arr = img_data.copy()
        nz = arr != 0
        if np.any(nz):
            m = float(np.mean(arr[nz]))
            s = float(np.std(arr[nz]))
            if s > 0:
                arr = (arr - m) / s
                arr[~nz] = 0.0
        t = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(device)
        with torch.no_grad():
            out = sliding_window_inference(
                t,
                tuple(cfg.get("roi_size", [96, 96, 96])),
                cfg.get("sw_batch_size", 1),
                model,
                overlap=cfg.get("overlap", 0.5),
                mode=cfg.get("mode", "gaussian"),
            )
        return torch.argmax(out, dim=1)[0].cpu().numpy().astype(np.uint8)

    def _load_swin_unetr(self, weights_path: str, sw_cfg: dict) -> None:
        import torch
        from monai.networks.nets import SwinUNETR

        in_channels = sw_cfg.get("in_channels", 1)
        out_channels = sw_cfg.get("out_channels", 6)
        feature_size = sw_cfg.get("feature_size", 48)
        roi_size = tuple(sw_cfg.get("roi_size", [96, 96, 96]))
        use_checkpoint = sw_cfg.get("use_checkpoint", False)
        device_str = self._config["inference"].get("device", "cuda")
        if device_str == "auto":
            device_str = "cuda" if torch.cuda.is_available() else "cpu"
        self._sw_device = torch.device(device_str)

        model = SwinUNETR(
            img_size=roi_size,  # required in MONAI 1.4; deprecated in 1.5+
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
        model.to(self._sw_device)
        model.eval()
        self._sw_model = model

        sha = hashlib.sha256()
        with open(weights_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        self._model_checksum_cache = sha.hexdigest()[:16]
        logger.info("swin_unetr_loaded", path=weights_path, device=str(self._sw_device))

    def _run_swin_unetr(self, img_data: np.ndarray, sw_cfg: dict) -> np.ndarray:
        import torch
        from monai.inferers import sliding_window_inference

        arr = img_data.copy()
        nonzero_mask = arr != 0
        if np.any(nonzero_mask):
            mean_val = float(np.mean(arr[nonzero_mask]))
            std_val = float(np.std(arr[nonzero_mask]))
            if std_val > 0:
                arr = (arr - mean_val) / std_val
                arr[~nonzero_mask] = 0.0

        img_tensor = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(self._sw_device)
        roi_size = tuple(sw_cfg.get("roi_size", [96, 96, 96]))
        sw_batch_size = sw_cfg.get("sw_batch_size", 1)
        overlap = sw_cfg.get("overlap", 0.5)
        mode = sw_cfg.get("mode", "gaussian")

        with torch.no_grad():
            output = sliding_window_inference(
                img_tensor, roi_size, sw_batch_size, self._sw_model,
                overlap=overlap, mode=mode,
            )

        return torch.argmax(output, dim=1)[0].cpu().numpy().astype(np.uint8)

    def _get_device(self):
        if self._device is not None:
            return self._device
        try:
            import torch
            device_config = self._config["inference"]["device"]
            if device_config == "auto":
                use_cuda = False
                if torch.cuda.is_available():
                    try:
                        torch.cuda.get_device_name(0)
                        use_cuda = True
                    except Exception:
                        pass
                self._device = torch.device("cuda" if use_cuda else "cpu")
            else:
                self._device = torch.device(device_config)
        except Exception:
            self._device = None
        logger.info("inference_device", device=str(self._device))
        return self._device

    def _load_model(self):
        """Attempt to load real model weights; returns None to trigger synthetic fallback."""
        if self._model is not None:
            return self._model

        custom_path = self._config["model"].get("custom_weights_path")
        if not custom_path or not Path(custom_path).exists():
            logger.info("no_custom_weights_found_using_synthetic_inference")
            return None

        try:
            import torch
            from monai.networks.nets import SegResNet

            device = self._get_device()
            logger.info("loading_custom_weights", path=custom_path)
            model = SegResNet(
                blocks_down=[1, 2, 2, 4],
                blocks_up=[1, 1, 1],
                init_filters=16,
                in_channels=1,
                out_channels=5,
                dropout_prob=0.2,
            )
            state_dict = torch.load(custom_path, map_location="cpu", weights_only=False)
            if "state_dict" in state_dict:
                state_dict = state_dict["state_dict"]
            if "model" in state_dict:
                state_dict = state_dict["model"]
            model.load_state_dict(state_dict, strict=False)
            model = model.to(device)
            model.eval()
            self._model = model
            logger.info("abdomen_model_loaded", path=custom_path, device=str(device))
            return model
        except Exception as exc:
            logger.warning("custom_weights_load_failed", error=str(exc))
            return None

    def _get_model_checksum(self) -> str:
        if self._model_checksum_cache:
            return self._model_checksum_cache
        arch = self._config["model"].get("architecture", "segresnet")
        if arch == "totalsegmentator_mr":
            self._model_checksum_cache = f"totalsegmentator_{self._config['model'].get('totalseg_task', 'total_mr')}"
        else:
            custom_path = self._config["model"].get("custom_weights_path")
            if custom_path and Path(custom_path).exists():
                sha = hashlib.sha256()
                with open(custom_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        sha.update(chunk)
                self._model_checksum_cache = sha.hexdigest()[:16]
            else:
                self._model_checksum_cache = "synthetic"
        return self._model_checksum_cache

    # =====================================================================
    # Pipeline Phases
    # =====================================================================

    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        logger.info(
            "abdomen_mri_preprocess_start",
            study_uid=study.study_instance_uid,
            series_count=len(series),
        )

        classified = self._classify_sequences(series)
        qa_flags = []
        qa_details = {}

        required = ["T2_HASTE", "T1_IP"]
        missing = [s for s in required if s not in classified]
        if missing:
            qa_flags.append("missing_sequence")
            qa_details["missing_sequences"] = missing
            logger.warning("missing_required_sequences", missing=missing,
                           classified=list(classified.keys()))

        loop = event_loop or asyncio.get_event_loop()
        nifti_dir = os.path.join(working_dir, "nifti")
        os.makedirs(nifti_dir, exist_ok=True)

        downloaded_niftis: dict[str, str] = {}
        for seq_name, seq_series in classified.items():
            nifti_path = os.path.join(nifti_dir, f"{seq_name}.nii.gz")
            try:
                loop.run_until_complete(
                    pacs.download_series_as_nifti(
                        study.study_instance_uid,
                        seq_series.series_instance_uid,
                        nifti_path,
                    )
                )
                downloaded_niftis[seq_name] = nifti_path
            except Exception as exc:
                logger.warning("series_download_failed", seq=seq_name, error=str(exc))

        # Fallback: grab the first series if nothing classified
        if not downloaded_niftis and series:
            fallback_path = os.path.join(nifti_dir, "FALLBACK.nii.gz")
            loop.run_until_complete(
                pacs.download_series_as_nifti(
                    study.study_instance_uid,
                    series[0].series_instance_uid,
                    fallback_path,
                )
            )
            downloaded_niftis["FALLBACK"] = fallback_path
            qa_flags.append("missing_sequence")
            qa_details["fallback_series"] = series[0].series_description

        if not downloaded_niftis:
            raise ValueError("No series could be downloaded for processing")

        # Prefer T2_HASTE then T1_IP as primary
        primary_seq = next(
            (p for p in ["T2_HASTE", "T1_IP", "T1_OP", "DCE", "DWI", "FALLBACK"]
             if p in downloaded_niftis),
            next(iter(downloaded_niftis)),
        )
        first_nifti = downloaded_niftis[primary_seq]

        spacing_qa = self._check_spacing(first_nifti)
        qa_flags.extend(spacing_qa.get("flags", []))
        qa_details.update(spacing_qa.get("details", {}))

        motion_qa = self._check_motion_artifacts(first_nifti)
        qa_flags.extend(motion_qa.get("flags", []))
        qa_details.update(motion_qa.get("details", {}))

        preprocessed_dir = os.path.join(working_dir, "preprocessed")
        os.makedirs(preprocessed_dir, exist_ok=True)
        input_path = os.path.join(preprocessed_dir, "input_1ch.nii.gz")
        self._build_single_channel_input(downloaded_niftis[primary_seq], input_path)

        sequences_used = list(downloaded_niftis.keys())

        logger.info(
            "abdomen_mri_preprocess_complete",
            primary_sequence=primary_seq,
            sequences_used=sequences_used,
            qa_flags=qa_flags,
        )

        return {
            "input_path": input_path,
            "original_nifti_path": first_nifti,
            "primary_sequence": primary_seq,
            "sequences_used": sequences_used,
            "classified_sequences": {k: v.series_instance_uid for k, v in classified.items()},
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    def _run_totalsegmentator(self, input_path: str, working_dir: str) -> np.ndarray:
        """Run TotalSegmentator MRI inference (nnU-Net, real trained weights)."""
        from totalsegmentator.python_api import totalsegmentator as ts_run

        cfg_model = self._config["model"]
        task = cfg_model.get("totalseg_task", "total_mr")
        weights_dir = cfg_model.get("totalseg_weights_dir", "/model_cache/totalsegmentator")
        organ_map: dict = cfg_model.get("organ_map", {})

        import torch
        device = "gpu" if torch.cuda.is_available() else "cpu"

        ts_out = os.path.join(working_dir, "totalseg_output")
        os.makedirs(ts_out, exist_ok=True)

        logger.info("totalsegmentator_start", task=task, device=device)
        ts_run(
            input=Path(input_path),
            output=Path(ts_out),
            task=task,
            device=device,
            quiet=True,
            weights_dir=Path(weights_dir) if weights_dir else None,
        )

        ref = nib.load(input_path)
        seg_array = np.zeros(ref.shape[:3], dtype=np.uint8)

        for organ_name, label_id in organ_map.items():
            organ_path = os.path.join(ts_out, f"{organ_name}.nii.gz")
            if os.path.exists(organ_path):
                mask = nib.load(organ_path).get_fdata() > 0.5
                seg_array[mask] = int(label_id)
            else:
                logger.warning("totalseg_organ_file_missing", organ=organ_name, path=organ_path)

        logger.info("totalsegmentator_complete", labels=np.unique(seg_array).tolist())
        return seg_array

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("abdomen_mri_inference_start")

        img_nib = nib.load(preprocessed["input_path"])
        affine = img_nib.affine

        qa_flags = list(preprocessed.get("qa_flags", []))
        architecture = self._config["model"].get("architecture", "segresnet")
        sw_cfg = self._config.get("swin_unetr", {})

        seg_array = None
        inference_method = None

        if self._sw_model is not None:
            try:
                img_data = img_nib.get_fdata().astype(np.float32)
                seg_array = self._run_swin_unetr(img_data, sw_cfg)
                inference_method = "swin_unetr"
            except Exception as exc:
                logger.warning("swin_unetr_inference_failed_falling_back", error=str(exc))

        if seg_array is None:
            if architecture == "totalsegmentator_mr":
                seg_array = self._run_totalsegmentator(preprocessed["input_path"], working_dir)
                inference_method = "totalsegmentator" if inference_method is None else "totalsegmentator_fallback"
            else:
                img_data = img_nib.get_fdata().astype(np.float32)
                model = self._load_model()
                if model is not None:
                    seg_array = self._run_model_inference(model, img_data)
                    inference_method = "segresnet" if inference_method is None else "segresnet_fallback"
                else:
                    logger.info("using_synthetic_inference")
                    seg_array = self._synthetic_inference(img_data)
                    qa_flags.append("no_model_weights")
                    inference_method = "synthetic" if inference_method is None else "synthetic_fallback"

        # ── Dedicated lesion detection (strictly modular) ─────────────────────
        lesion_active = self._lesion_model is not None
        lesion_mask_array = None
        if lesion_active:
            try:
                lesion_pred = self._run_aux_model(
                    self._lesion_model, self._lesion_device,
                    img_nib.get_fdata().astype(np.float32),
                    self._config.get("lesion_detection", {}),
                )
                lesion_mask_array = (lesion_pred > 0).astype(np.uint8)
                logger.info("lesion_model_applied", raw_lesion_voxels=int(lesion_mask_array.sum()))
            except Exception as exc:
                logger.warning("lesion_model_failed", error=str(exc))
                lesion_active = False
                lesion_mask_array = None

        seg_path = os.path.join(working_dir, "segmentation_raw.nii.gz")
        seg_img = nib.Nifti1Image(seg_array, affine=affine)
        nib.save(seg_img, seg_path)

        logger.info(
            "abdomen_mri_inference_complete",
            seg_shape=list(seg_array.shape),
            unique_labels=np.unique(seg_array).tolist(),
            inference_method=inference_method,
            lesion_detection_active=lesion_active,
        )

        return {
            "segmentation_path": seg_path,
            "segmentation_array": seg_array,
            "affine": affine,
            "image_shape": list(seg_array.shape),
            "inference_method": inference_method,
            "lesion_active": lesion_active,
            "lesion_mask_array": lesion_mask_array,
            **{**preprocessed, "qa_flags": qa_flags},
        }

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("abdomen_mri_postprocess_start")

        seg_array = inference_output["segmentation_array"]
        affine = inference_output["affine"]
        label_map = self._config["postprocessing"]["label_map"]
        min_vol = self._config["postprocessing"].get("min_structure_volume_ml", 0.5)
        ref_vols = self._config.get("reference_volumes_ml", {})

        if isinstance(affine, np.ndarray):
            voxel_spacing = np.abs(np.diag(affine[:3, :3]))
        else:
            voxel_spacing = np.array([1.0, 1.0, 1.0])
        voxel_volume_ml = float(np.prod(voxel_spacing)) / 1000.0

        seg_clean = seg_array.copy()

        if self._config["postprocessing"].get("apply_connected_components", False):
            seg_clean = self._apply_connected_components(
                seg_clean,
                label_map,
                self._config["postprocessing"].get("largest_component_only_labels", []),
            )

        # Remove fragments below volume threshold — driven by label_map, not hardcoded
        non_bg_labels = sorted(int(k) for k in label_map if int(k) != 0)
        for label_id in non_bg_labels:
            if label_id not in np.unique(seg_clean):
                continue
            label_mask = (seg_clean == label_id).astype(np.int32)
            labeled, num_features = ndimage.label(label_mask)
            for comp_id in range(1, num_features + 1):
                comp_volume = float(np.sum(labeled == comp_id)) * voxel_volume_ml
                if comp_volume < min_vol:
                    seg_clean[labeled == comp_id] = 0

        # ---- Per-organ volumes ----
        # label_map: {0: background, 1: liver, 2: spleen, 3: right_kidney,
        #              4: left_kidney, 5: pancreas}
        label_to_name = {int(k): v for k, v in label_map.items() if int(k) != 0}
        volumes_ml: dict[str, float] = {}
        for label_id, organ_name in label_to_name.items():
            count = int(np.sum(seg_clean == label_id))
            volumes_ml[organ_name] = round(float(count) * voxel_volume_ml, 1)

        total_parenchymal_volume_ml = round(sum(volumes_ml.values()), 1)
        organ_count_segmented = sum(1 for v in volumes_ml.values() if v > 0)

        liver_vol = volumes_ml.get("liver", 0.0)
        spleen_vol = volumes_ml.get("spleen", 0.0)
        liver_normal_max = float(ref_vols.get("liver_normal_max", 1800.0))
        spleen_normal_max = float(ref_vols.get("spleen_normal_max", 315.0))
        hepatomegaly_suspected = liver_vol > liver_normal_max
        splenomegaly_suspected = spleen_vol > spleen_normal_max

        # Per-organ reference-range characterization (screening — not diagnostic).
        ranges = dict(self._ORGAN_REF_RANGES_ML)
        for organ, rng in (self._config.get("organ_reference_ranges_ml") or {}).items():
            try:
                ranges[organ] = (float(rng[0]), float(rng[1]))
            except Exception:
                pass
        organ_findings = self._characterize_organs(volumes_ml, ranges)
        abnormal_findings = [f for f in organ_findings if f.get("status") != "normal"]

        # ── Dedicated lesion detection result (mirrors chest_mri Problem C) ────
        # lesion_detected reflects the dedicated lesion model when active; with no
        # lesion model we do NOT infer lesions from organ volumes — we only report
        # the reference-range organ characterization above.
        lesion_active = bool(inference_output.get("lesion_active", False))
        lesion_mask_array = inference_output.get("lesion_mask_array")
        lesion_count = 0
        lesion_total_ml = 0.0
        lesion_seg_clean = None
        if lesion_active and lesion_mask_array is not None:
            lcfg = self._config.get("lesion_detection", {})
            min_les = lcfg.get("min_lesion_volume_ml", 0.5)
            labeled, n_les = ndimage.label(lesion_mask_array)
            kept = np.zeros_like(lesion_mask_array)
            for cid in range(1, n_les + 1):
                comp = labeled == cid
                v = float(np.sum(comp)) * voxel_volume_ml
                if v >= min_les:
                    kept[comp] = 1
                    lesion_count += 1
                    lesion_total_ml += v
            lesion_seg_clean = kept

        if lesion_active:
            lesion_detected = lesion_count > 0
            lesion_detection_method = "swin_unetr"
        else:
            lesion_detected = False
            lesion_detection_method = "not_active"

        # Save artifacts
        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        seg_nifti_path = os.path.join(artifacts_dir, "segmentation.nii.gz")
        seg_img = nib.Nifti1Image(seg_clean.astype(np.uint8), affine=affine)
        nib.save(seg_img, seg_nifti_path)

        report = {
            "summary": {
                "lesion_detected": lesion_detected,
                "lesion_detection_method": lesion_detection_method,
                "lesion_count": lesion_count,
                "hepatomegaly_suspected": hepatomegaly_suspected,
                "splenomegaly_suspected": splenomegaly_suspected,
                "abnormal_findings": abnormal_findings,
                "organ_count_segmented": organ_count_segmented,
                "segmentation_labels": {
                    str(k): v for k, v in label_map.items() if int(k) != 0
                },
                "sequences_used": inference_output.get("sequences_used", []),
                "inference_method": inference_output.get("inference_method", "unknown"),
                "processing_notes": self._generate_processing_notes(
                    inference_output.get("qa_flags", []),
                    volumes_ml,
                    hepatomegaly_suspected,
                    splenomegaly_suspected,
                    organ_count_segmented,
                    abnormal_findings,
                    lesion_active=lesion_active,
                    lesion_count=lesion_count,
                ),
            },
            "measurements": {
                "volumes_ml": volumes_ml,
                "organ_findings": organ_findings,
                "total_parenchymal_volume_ml": total_parenchymal_volume_ml,
                "lesion_count": lesion_count,
                "lesion_volume_ml": round(lesion_total_ml, 1),
                "voxel_spacing_mm": [round(float(s), 3) for s in voxel_spacing],
                "image_dimensions": inference_output.get("image_shape", []),
            },
        }

        report_path = os.path.join(artifacts_dir, "report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        inference_method = inference_output.get("inference_method", "")
        model_version = self._config["model"].get("version", "1.0.0")
        qa_flags = inference_output.get("qa_flags", [])
        architecture = self._config["model"].get("architecture", "segresnet")
        if inference_method and inference_method.startswith("swin_unetr"):
            sw_path = self._config.get("swin_unetr", {}).get("custom_weights_path", "unknown")
            model_version_str = f"abdomen_mri_swinunetr_{Path(sw_path).stem}"
        elif architecture == "totalsegmentator_mr":
            task = self._config["model"].get("totalseg_task", "total_mr")
            model_version_str = f"totalsegmentator_{task}_v{model_version}"
        elif "no_model_weights" in qa_flags:
            model_version_str = f"abdomen_mri_synthetic_v{model_version}"
        else:
            model_version_str = f"abdomen_mri_v{model_version}"

        model_checksum = self._get_model_checksum()

        qa_details = dict(inference_output.get("qa_details", {}))
        qa_details["segmentation_stats"] = {
            "unique_labels": [int(x) for x in np.unique(seg_clean).tolist()],
            "organ_count_segmented": organ_count_segmented,
            "total_parenchymal_volume_ml": total_parenchymal_volume_ml,
            "volumes_ml": volumes_ml,
            "voxel_volume_ml": round(voxel_volume_ml, 6),
        }

        # Generate preview overlay images
        preview_artifacts = []
        try:
            from app.services.preview_generator import generate_preview_pngs

            bg_path = inference_output.get("input_path") or inference_output.get(
                "original_nifti_path"
            )
            if bg_path and os.path.exists(bg_path):
                preview_artifacts = generate_preview_pngs(
                    background_nifti_path=bg_path,
                    segmentation_nifti_path=seg_nifti_path,
                    output_dir=artifacts_dir,
                )
                logger.info("preview_images_generated", count=len(preview_artifacts))
        except Exception as exc:
            logger.warning("preview_generation_failed", error=str(exc))

        # Lesion mask artifact (only when the dedicated lesion model detected any).
        lesion_artifacts = []
        if lesion_seg_clean is not None and int(lesion_seg_clean.sum()) > 0:
            lesion_path = os.path.join(artifacts_dir, "lesion_mask.nii.gz")
            nib.save(nib.Nifti1Image(lesion_seg_clean.astype(np.uint8), affine), lesion_path)
            lesion_artifacts = [{
                "name": "lesion_mask.nii.gz",
                "artifact_type": "segmentation_nifti",
                "local_path": lesion_path,
                "content_type": "application/gzip",
            }]

        result = {
            "summary": report["summary"],
            "measurements": report["measurements"],
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": model_version_str,
            "model_checksum": model_checksum,
            "artifacts": [
                {
                    "name": "segmentation.nii.gz",
                    "artifact_type": "segmentation_nifti",
                    "local_path": seg_nifti_path,
                    "content_type": "application/gzip",
                },
                {
                    "name": "report.json",
                    "artifact_type": "report_json",
                    "local_path": report_path,
                    "content_type": "application/json",
                },
                *preview_artifacts,
                *lesion_artifacts,
            ],
        }

        logger.info(
            "abdomen_mri_postprocess_complete",
            organ_count_segmented=organ_count_segmented,
            total_parenchymal_volume_ml=total_parenchymal_volume_ml,
            hepatomegaly_suspected=hepatomegaly_suspected,
            splenomegaly_suspected=splenomegaly_suspected,
            lesion_detected=lesion_detected,
            qa_flags=qa_flags,
        )

        return result

    # =====================================================================
    # Inference helpers
    # =====================================================================

    def _run_model_inference(self, model, img_data: np.ndarray) -> np.ndarray:
        """Run the loaded SegResNet model and return a uint8 label array."""
        import torch
        from monai.inferers import sliding_window_inference

        device = self._get_device()
        img_tensor = torch.from_numpy(img_data).unsqueeze(0).unsqueeze(0).to(device)

        roi_size = tuple(self._config["inference"]["sliding_window"]["roi_size"])
        sw_batch_size = self._config["inference"]["sliding_window"]["sw_batch_size"]
        overlap = self._config["inference"]["sliding_window"]["overlap"]

        with torch.no_grad():
            if self._config["inference"].get("mixed_precision", True) and device.type == "cuda":
                with torch.amp.autocast("cuda"):
                    output = sliding_window_inference(
                        img_tensor, roi_size, sw_batch_size, model,
                        overlap=overlap, mode="gaussian",
                    )
            else:
                output = sliding_window_inference(
                    img_tensor, roi_size, sw_batch_size, model,
                    overlap=overlap, mode="gaussian",
                )

        pred = torch.argmax(output, dim=1)[0].cpu().numpy().astype(np.uint8)
        return pred

    def _synthetic_inference(self, img_data: np.ndarray) -> np.ndarray:
        """Intensity-thresholding synthetic segmentation for abdominal organs.

        Labels:
          1 = liver        (large, right upper quadrant; moderate-high T2 signal)
          2 = spleen       (medium, left upper quadrant; bright T2)
          3 = right_kidney (right flank; high T2 cortex with dark medulla)
          4 = left_kidney  (left flank)
          5 = pancreas     (small retroperitoneal structure; bright T2)

        Strategy:
        - Percentile-threshold the normalised input into tissue tiers.
        - Use spatial heuristics (left/right, superior/inferior) to assign
          each large connected component to the most plausible organ.
        - The largest component in the upper-right quadrant → liver.
        - The largest component in the upper-left quadrant → spleen.
        - Two bilateral medium components in the flanks → kidneys.
        - A small bright component in the mid-abdomen → pancreas.
        """
        arr = img_data.copy()
        seg = np.zeros(arr.shape, dtype=np.uint8)

        nonzero = arr[arr > 0]
        if nonzero.size == 0:
            return seg

        p25 = float(np.percentile(nonzero, 25))
        p45 = float(np.percentile(nonzero, 45))
        p60 = float(np.percentile(nonzero, 60))
        p75 = float(np.percentile(nonzero, 75))
        p88 = float(np.percentile(nonzero, 88))

        # Volume spatial midpoints
        mid_si = arr.shape[0] // 2   # superior–inferior (slice axis)
        mid_ap = arr.shape[1] // 2   # anterior–posterior
        mid_lr = arr.shape[2] // 2   # left–right

        # --- Candidate tissue mask: p45–p75 (solid organs) ---
        tissue_raw = ((arr >= p45) & (arr < p75)).astype(np.int32)
        tissue_labeled, n_tissue = ndimage.label(tissue_raw)

        # Collect component metadata
        comps: list[dict] = []
        if n_tissue > 0:
            for cid in range(1, n_tissue + 1):
                mask = tissue_labeled == cid
                size = int(np.sum(mask))
                com = ndimage.center_of_mass(mask)
                comps.append({"id": cid, "size": size, "com": com, "mask": mask})
            comps.sort(key=lambda c: c["size"], reverse=True)

        # --- Label 1: Liver — largest component in the right upper quadrant ---
        liver_assigned = False
        for comp in comps:
            com = comp["com"]
            # Superior half (com[0] < mid_si), right side (com[2] > mid_lr)
            if com[0] < mid_si and com[2] >= mid_lr:
                seg[comp["mask"]] = 1
                liver_assigned = True
                break
        if not liver_assigned and comps:
            seg[comps[0]["mask"]] = 1

        # Refresh remaining unlabelled components
        remaining = [c for c in comps if not np.any(seg[c["mask"]] != 0)]

        # --- Label 2: Spleen — next large component in the left upper quadrant ---
        spleen_assigned = False
        for comp in remaining:
            com = comp["com"]
            if com[0] < mid_si and com[2] < mid_lr:
                seg[comp["mask"]] = 2
                spleen_assigned = True
                break
        if not spleen_assigned and remaining:
            seg[remaining[0]["mask"]] = 2

        remaining = [c for c in comps if not np.any(seg[c["mask"]] != 0)]

        # --- Labels 3 & 4: Kidneys — bilateral medium components in flanks ---
        right_kidney_assigned = False
        left_kidney_assigned = False
        kidney_candidates = [c for c in remaining if c["size"] < (comps[0]["size"] // 3)]
        for comp in kidney_candidates:
            com = comp["com"]
            if not right_kidney_assigned and com[2] >= mid_lr:
                seg[comp["mask"]] = 3
                right_kidney_assigned = True
            elif not left_kidney_assigned and com[2] < mid_lr:
                seg[comp["mask"]] = 4
                left_kidney_assigned = True
            if right_kidney_assigned and left_kidney_assigned:
                break

        # --- Label 5: Pancreas — small bright structure, mid-abdomen ---
        pancreas_raw = ((arr >= p75) & (arr < p88) & (seg == 0)).astype(np.int32)
        pancreas_labeled, n_pancreas = ndimage.label(pancreas_raw)
        if n_pancreas > 0:
            sizes = ndimage.sum(pancreas_raw, pancreas_labeled, range(1, n_pancreas + 1))
            # Pancreas is small — pick the largest candidate but cap at 200 mL proxy
            # by excluding very large components
            valid_ids = [
                i + 1 for i, s in enumerate(sizes)
                if s < (sum(sizes) * 0.4)  # exclude dominant tissue blobs
            ]
            if valid_ids:
                best_id = max(valid_ids, key=lambda i: sizes[i - 1])
                seg[pancreas_labeled == best_id] = 5
            else:
                # Fallback: just use largest
                largest_id = int(np.argmax(sizes)) + 1
                seg[pancreas_labeled == largest_id] = 5

        logger.info(
            "synthetic_inference_complete",
            unique_labels=np.unique(seg).tolist(),
            shape=list(seg.shape),
        )
        return seg

    # =====================================================================
    # Preprocessing helpers
    # =====================================================================

    def _build_single_channel_input(self, nifti_path: str, output_path: str):
        """Resample to target spacing, z-score normalise, save as single-channel NIfTI."""
        target_spacing = self._config["preprocessing"]["target_spacing"]
        img = sitk.ReadImage(nifti_path)
        original_spacing = img.GetSpacing()
        original_size = img.GetSize()
        new_size = [
            int(round(osz * osp / nsp))
            for osz, osp, nsp in zip(original_size, original_spacing, target_spacing)
        ]
        resampler = sitk.ResampleImageFilter()
        resampler.SetOutputSpacing(target_spacing)
        resampler.SetSize(new_size)
        resampler.SetOutputDirection(img.GetDirection())
        resampler.SetOutputOrigin(img.GetOrigin())
        resampler.SetInterpolator(sitk.sitkLinear)
        resampler.SetDefaultPixelValue(0)
        resampled = resampler.Execute(img)

        arr = sitk.GetArrayFromImage(resampled).astype(np.float32)
        nonzero_mask = arr > 0
        if np.sum(nonzero_mask) > 0:
            mean_val = float(np.mean(arr[nonzero_mask]))
            std_val = float(np.std(arr[nonzero_mask]))
            if std_val > 0:
                arr = (arr - mean_val) / std_val
                arr[~nonzero_mask] = 0.0

        direction = np.array(img.GetDirection()).reshape(3, 3)
        spacing_arr = np.array(target_spacing)
        origin = np.array(img.GetOrigin())
        affine = np.eye(4)
        affine[:3, :3] = direction * spacing_arr
        affine[:3, 3] = origin

        nib_img = nib.Nifti1Image(arr, affine=affine)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        nib.save(nib_img, output_path)
        logger.info("single_channel_input_built", shape=list(arr.shape))

    def _classify_sequences(self, series: list[Series]) -> dict[str, Series]:
        classified: dict[str, Series] = {}
        for s in series:
            desc = (s.series_description or "").strip()
            protocol = (s.protocol_name or "").strip() if hasattr(s, "protocol_name") else ""
            combined = f"{desc} {protocol}"
            for seq_name, patterns in SEQUENCE_PATTERNS.items():
                if seq_name in classified:
                    continue
                for pat in patterns:
                    if re.search(pat, combined):
                        classified[seq_name] = s
                        break
        logger.info(
            "sequence_classification",
            result={k: v.series_description for k, v in classified.items()},
        )
        return classified

    def _check_spacing(self, nifti_path: str) -> dict[str, Any]:
        flags = []
        details = {}
        img = sitk.ReadImage(nifti_path)
        spacing = img.GetSpacing()
        size = img.GetSize()

        qc = self._config["quality_checks"]

        for i, sp in enumerate(spacing):
            if sp < qc["min_expected_spacing_mm"] or sp > qc["max_expected_spacing_mm"]:
                flags.append("spacing_inconsistency")
                details["spacing_issue"] = (
                    f"Axis {i} spacing {sp:.3f}mm outside range "
                    f"[{qc['min_expected_spacing_mm']}, {qc['max_expected_spacing_mm']}]"
                )
                break

        if min(size) < qc["min_slices"]:
            flags.append("incomplete_coverage")
            details["coverage_issue"] = (
                f"Min dimension {min(size)} < threshold {qc['min_slices']}"
            )

        anisotropy = max(spacing) / (min(spacing) + 1e-8)
        if anisotropy > qc["max_spacing_anisotropy"]:
            flags.append("spacing_inconsistency")
            details["anisotropy"] = round(anisotropy, 2)

        slice_gaps = [i for i in range(len(spacing)) if spacing[i] > 2 * min(spacing)]
        if slice_gaps:
            flags.append("slice_gap")
            details["slice_gap_axes"] = slice_gaps

        details["actual_spacing_mm"] = [round(s, 3) for s in spacing]
        details["image_size"] = list(size)
        return {"flags": flags, "details": details}

    def _check_motion_artifacts(self, nifti_path: str) -> dict[str, Any]:
        flags = []
        details = {}
        img = sitk.ReadImage(nifti_path)
        arr = sitk.GetArrayFromImage(img).astype(np.float32)

        if arr.ndim < 3:
            return {"flags": [], "details": {}}

        mid = arr.shape[0] // 2
        roi = arr[max(0, mid - 5):mid + 5]
        if roi.size == 0:
            return {"flags": [], "details": {}}

        roi_flat = roi[roi > 0]
        if roi_flat.size < 100:
            return {"flags": [], "details": {}}

        mean_signal = float(np.mean(roi_flat))
        edge_energy = float(np.mean(np.abs(np.diff(roi_flat))))
        normalized_edge = edge_energy / (mean_signal + 1e-8)

        details["coefficient_of_variation"] = round(
            float(np.std(roi_flat)) / (mean_signal + 1e-8), 4
        )
        details["normalized_edge_energy"] = round(normalized_edge, 4)

        if normalized_edge > self._config["quality_checks"]["motion_artifact_threshold"]:
            flags.append("motion_artifact")
            details["motion_assessment"] = (
                "Elevated edge energy suggesting possible motion"
            )

        return {"flags": flags, "details": details}

    # =====================================================================
    # Postprocessing helpers
    # =====================================================================

    @staticmethod
    def _apply_connected_components(
        seg: np.ndarray,
        label_map: dict,
        largest_only_labels: list[int],
    ) -> np.ndarray:
        result = seg.copy()
        for label_id in largest_only_labels:
            mask = (result == label_id).astype(np.int32)
            if np.sum(mask) == 0:
                continue
            labeled, num = ndimage.label(mask)
            if num <= 1:
                continue
            sizes = ndimage.sum(mask, labeled, range(1, num + 1))
            largest = int(np.argmax(sizes)) + 1
            result[np.logical_and(labeled != largest, labeled > 0)] = 0
        return result

    # Adult parenchymal volume reference ranges (mL), approximate — for
    # abnormality SCREENING only, not diagnosis. Override via config
    # `organ_reference_ranges_ml: {organ: [low, high]}`.
    _ORGAN_REF_RANGES_ML: dict[str, tuple[float, float]] = {
        "liver": (1200.0, 1800.0),
        "spleen": (100.0, 315.0),
        "right_kidney": (110.0, 220.0),
        "left_kidney": (110.0, 220.0),
        "pancreas": (40.0, 120.0),
    }

    @staticmethod
    def _characterize_organs(
        volumes_ml: dict[str, float], ranges: dict[str, tuple[float, float]]
    ) -> list[dict[str, Any]]:
        """Compare each segmented organ volume to its reference range and classify
        as small / normal / enlarged with a severity. Deterministic, derived from
        the segmentation — a screening characterization, not a diagnosis."""
        findings: list[dict[str, Any]] = []
        for organ, vol in volumes_ml.items():
            if vol <= 0:
                continue  # not segmented in this study
            rng = ranges.get(organ)
            if not rng:
                continue
            low, high = float(rng[0]), float(rng[1])
            label = organ.replace("_", " ")
            if vol > high:
                dev = (vol - high) / high
                sev = "mild" if dev < 0.25 else "moderate" if dev < 0.5 else "marked"
                findings.append({
                    "organ": organ, "volume_ml": vol, "status": "enlarged",
                    "severity": sev, "reference_ml": [low, high],
                    "note": f"{label} {vol:.0f} mL exceeds upper reference {high:.0f} mL "
                            f"({sev} enlargement)",
                })
            elif vol < low:
                dev = (low - vol) / low
                sev = "mild" if dev < 0.25 else "moderate" if dev < 0.5 else "marked"
                findings.append({
                    "organ": organ, "volume_ml": vol, "status": "small",
                    "severity": sev, "reference_ml": [low, high],
                    "note": f"{label} {vol:.0f} mL below lower reference {low:.0f} mL "
                            f"({sev} volume loss / atrophy)",
                })
            else:
                findings.append({
                    "organ": organ, "volume_ml": vol, "status": "normal",
                    "reference_ml": [low, high],
                })
        return findings

    @staticmethod
    def _generate_processing_notes(
        qa_flags: list[str],
        volumes_ml: dict[str, float],
        hepatomegaly_suspected: bool,
        splenomegaly_suspected: bool,
        organ_count: int,
        abnormal_findings: list[dict[str, Any]] | None = None,
        lesion_active: bool = False,
        lesion_count: int = 0,
    ) -> str:
        notes = []
        if organ_count > 0:
            organ_list = ", ".join(
                f"{k} ({v:.0f} mL)" for k, v in volumes_ml.items() if v > 0
            )
            notes.append(f"Segmented {organ_count} organ(s): {organ_list}.")
        else:
            notes.append("No abdominal organs detected in this study.")
        # Lesion reporting: dedicated model vs. reference-range screening only.
        if lesion_active:
            if lesion_count > 0:
                notes.append(
                    f"Dedicated lesion model detected {lesion_count} lesion(s); "
                    "clinical correlation recommended."
                )
            else:
                notes.append("Dedicated lesion model found no lesions.")
        else:
            notes.append(
                "No dedicated lesion-detection model active — only organ-volume "
                "reference-range screening was performed (not lesion detection)."
            )
        if hepatomegaly_suspected:
            liver_vol = volumes_ml.get("liver", 0.0)
            notes.append(
                f"Liver volume ({liver_vol:.0f} mL) exceeds the 95th-percentile "
                "reference threshold; hepatomegaly possible. Clinical correlation required."
            )
        if splenomegaly_suspected:
            spleen_vol = volumes_ml.get("spleen", 0.0)
            notes.append(
                f"Spleen volume ({spleen_vol:.0f} mL) exceeds the 95th-percentile "
                "reference threshold; splenomegaly possible. Clinical correlation required."
            )
        # Per-organ reference-range abnormalities beyond liver/spleen.
        for f in (abnormal_findings or []):
            if f.get("organ") in ("liver", "spleen"):
                continue  # already covered above
            notes.append(f["note"].capitalize() + ". Clinical correlation recommended.")
        if "missing_sequence" in qa_flags:
            notes.append(
                "One or more expected sequences were missing; "
                "available sequences were used as fallback."
            )
        if "motion_artifact" in qa_flags:
            notes.append("Possible motion artifacts detected; review segmentation carefully.")
        if "no_model_weights" in qa_flags:
            notes.append(
                "No trained model weights found; synthetic inference was used. "
                "Results are illustrative only and must not be used clinically."
            )
        if not qa_flags and organ_count > 0:
            notes.append("Processing completed normally with no quality concerns.")
        return " ".join(notes)
