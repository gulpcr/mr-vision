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

# Chest MRI sequence classification patterns.
SEQUENCE_PATTERNS = {
    "T2_HASTE": [
        r"(?i)haste",
        r"(?i)t2.*haste",
        r"(?i)haste.*t2",
        r"(?i)t2.*ss.*fse",
        r"(?i)ssfse",
        r"(?i)t2.*fiesta",
    ],
    "T1_GRE": [
        r"(?i)t1.*gre",
        r"(?i)gre.*t1",
        r"(?i)t1.*flash",
        r"(?i)flash.*t1",
        r"(?i)t1.*vibe",
        r"(?i)vibe",
        r"(?i)t1.*spgr",
        r"(?i)t1w.*3d",
    ],
    "STIR": [
        r"(?i)stir",
        r"(?i)t2.*stir",
        r"(?i)short.*tau",
        r"(?i)fat.*sat.*t2",
        r"(?i)t2.*fat.*sat",
    ],
    "TRUE_FISP": [
        r"(?i)true.*fisp",
        r"(?i)truefisp",
        r"(?i)fisp",
        r"(?i)bssfp",
        r"(?i)balanced.*ssfp",
        r"(?i)fiesta",
        r"(?i)trufi",
    ],
}


class Pipeline(BasePipeline):
    """Chest MRI segmentation pipeline.

    Segments four structures: right lung, left lung, heart, and aorta.
    When no trained model weights are available the pipeline falls back to
    synthetic inference (intensity-thresholding + morphological operations).

    Performs:
    - Sequence classification from DICOM series descriptions
    - NIfTI download (primary T2 HASTE + supplementary sequences)
    - QA checks for spacing, coverage, and motion artifacts
    - Segmentation (real model or synthetic fallback)
    - Volumetric measurements per organ and bilateral lung comparison
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

    def _load_swin_unetr(self, weights_path: str, sw_cfg: dict) -> None:
        import torch
        from monai.networks.nets import SwinUNETR

        in_channels = sw_cfg.get("in_channels", 1)
        out_channels = sw_cfg.get("out_channels", 5)
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
                out_channels=4,
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
            logger.info("chest_model_loaded", path=custom_path, device=str(device))
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
            "chest_mri_preprocess_start",
            study_uid=study.study_instance_uid,
            series_count=len(series),
        )

        classified = self._classify_sequences(series)
        qa_flags = []
        qa_details = {}

        if "T2_HASTE" not in classified:
            qa_flags.append("missing_sequence")
            qa_details["missing_sequences"] = [
                s for s in ["T2_HASTE"] if s not in classified
            ]
            logger.warning("missing_primary_sequence", classified=list(classified.keys()))

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

        primary_seq = next(
            (p for p in ["T2_HASTE", "STIR", "TRUE_FISP", "T1_GRE", "FALLBACK"]
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
            "chest_mri_preprocess_complete",
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
        logger.info("chest_mri_inference_start")

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

        seg_path = os.path.join(working_dir, "segmentation_raw.nii.gz")
        seg_img = nib.Nifti1Image(seg_array, affine=affine)
        nib.save(seg_img, seg_path)

        logger.info(
            "chest_mri_inference_complete",
            seg_shape=list(seg_array.shape),
            unique_labels=np.unique(seg_array).tolist(),
            inference_method=inference_method,
        )

        return {
            "segmentation_path": seg_path,
            "segmentation_array": seg_array,
            "affine": affine,
            "image_shape": list(seg_array.shape),
            "inference_method": inference_method,
            **{**preprocessed, "qa_flags": qa_flags},
        }

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("chest_mri_postprocess_start")

        seg_array = inference_output["segmentation_array"]
        affine = inference_output["affine"]
        label_map = self._config["postprocessing"]["label_map"]
        min_vol = self._config["postprocessing"].get("min_structure_volume_ml", 1.0)

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

        # ---- Measurements ----
        def vol_ml(label_id: int) -> float:
            return round(float(np.sum(seg_clean == label_id)) * voxel_volume_ml, 1)

        right_lung_vol = vol_ml(1)
        left_lung_vol = vol_ml(2)
        total_lung_vol = round(right_lung_vol + left_lung_vol, 1)
        heart_vol = vol_ml(3)
        aorta_vol = vol_ml(4)

        bilateral_analysis = right_lung_vol > 0 and left_lung_vol > 0
        lung_volume_ratio = (
            round(right_lung_vol / left_lung_vol, 3) if left_lung_vol > 0 else 0.0
        )

        # A lesion is suspected when a significant asymmetry is present
        # (ratio outside the normal 0.8–1.3 range) or total volume is very low.
        lesion_detected = (
            bilateral_analysis and (lung_volume_ratio < 0.6 or lung_volume_ratio > 1.6)
        ) or (
            bilateral_analysis and total_lung_vol < 500.0
        )

        # Save artifacts
        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        seg_nifti_path = os.path.join(artifacts_dir, "segmentation.nii.gz")
        seg_img = nib.Nifti1Image(seg_clean.astype(np.uint8), affine=affine)
        nib.save(seg_img, seg_nifti_path)

        report = {
            "summary": {
                "lesion_detected": lesion_detected,
                "bilateral_analysis": bilateral_analysis,
                "lung_volume_ratio": lung_volume_ratio,
                "segmentation_labels": {
                    str(k): v for k, v in label_map.items() if int(k) != 0
                },
                "sequences_used": inference_output.get("sequences_used", []),
                "inference_method": inference_output.get("inference_method", "unknown"),
                "processing_notes": self._generate_processing_notes(
                    inference_output.get("qa_flags", []),
                    right_lung_vol,
                    left_lung_vol,
                    heart_vol,
                    lesion_detected,
                    bilateral_analysis,
                ),
            },
            "measurements": {
                "right_lung_volume_ml": right_lung_vol,
                "left_lung_volume_ml": left_lung_vol,
                "total_lung_volume_ml": total_lung_vol,
                "heart_volume_ml": heart_vol,
                "aorta_volume_ml": aorta_vol,
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
            model_version_str = f"chest_mri_swinunetr_{Path(sw_path).stem}"
        elif architecture == "totalsegmentator_mr":
            task = self._config["model"].get("totalseg_task", "total_mr")
            model_version_str = f"totalsegmentator_{task}_v{model_version}"
        elif "no_model_weights" in qa_flags:
            model_version_str = f"chest_mri_synthetic_v{model_version}"
        else:
            model_version_str = f"chest_mri_v{model_version}"

        model_checksum = self._get_model_checksum()

        qa_details = dict(inference_output.get("qa_details", {}))
        qa_details["segmentation_stats"] = {
            "unique_labels": [int(x) for x in np.unique(seg_clean).tolist()],
            "right_lung_volume_ml": right_lung_vol,
            "left_lung_volume_ml": left_lung_vol,
            "total_lung_volume_ml": total_lung_vol,
            "heart_volume_ml": heart_vol,
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
            ],
        }

        logger.info(
            "chest_mri_postprocess_complete",
            right_lung_vol=right_lung_vol,
            left_lung_vol=left_lung_vol,
            heart_vol=heart_vol,
            lesion_detected=lesion_detected,
            bilateral_analysis=bilateral_analysis,
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
        """Intensity-thresholding synthetic segmentation for chest labels.

        Labels:
          1 = right_lung   (low signal, lateral right half of volume)
          2 = left_lung    (low signal, lateral left half of volume)
          3 = heart        (medium signal, central mediastinum)
          4 = aorta        (small tubular structure adjacent to heart)

        Strategy:
        - Air-filled lungs appear dark; threshold below the 40th percentile.
        - Heart is a large soft-tissue mass in the mediastinum (50th–80th pct).
        - Split the lung mask left/right at the volume midpoint along axis 2
          (left–right direction in RAS orientation).
        - Aorta is a small high-signal structure near the heart.
        """
        arr = img_data.copy()
        seg = np.zeros(arr.shape, dtype=np.uint8)

        nonzero = arr[arr > 0]
        if nonzero.size == 0:
            return seg

        p20 = float(np.percentile(nonzero, 20))
        p40 = float(np.percentile(nonzero, 40))
        p55 = float(np.percentile(nonzero, 55))
        p75 = float(np.percentile(nonzero, 75))
        p88 = float(np.percentile(nonzero, 88))

        mid_lr = arr.shape[2] // 2  # left–right split axis

        # ------------------------------------------------------------------
        # Label 1 & 2: Lungs — low signal regions, split left/right
        # ------------------------------------------------------------------
        lung_raw = (arr >= p20) & (arr < p40)
        lung_labeled, n_lung = ndimage.label(lung_raw.astype(np.int32))
        if n_lung > 0:
            sizes = ndimage.sum(lung_raw.astype(np.int32), lung_labeled, range(1, n_lung + 1))
            # Keep top 4 components to cover both lung lobes
            n_keep = min(n_lung, 4)
            top_ids = np.argsort(sizes)[::-1][:n_keep] + 1
            for lid in top_ids:
                comp_mask = lung_labeled == lid
                # Determine laterality by centre of mass along left–right axis
                com = ndimage.center_of_mass(comp_mask)
                if com[2] >= mid_lr:
                    seg[comp_mask] = 1  # right lung (high index = right in RAS)
                else:
                    seg[comp_mask] = 2  # left lung

        # ------------------------------------------------------------------
        # Label 3: Heart — medium-intensity central mass
        # ------------------------------------------------------------------
        heart_raw = ((arr >= p55) & (arr < p75) & (seg == 0)).astype(np.int32)
        heart_labeled, n_heart = ndimage.label(heart_raw)
        if n_heart > 0:
            sizes = ndimage.sum(heart_raw, heart_labeled, range(1, n_heart + 1))
            largest_id = int(np.argmax(sizes)) + 1
            seg[heart_labeled == largest_id] = 3

        # ------------------------------------------------------------------
        # Label 4: Aorta — small bright tubular structure near the heart
        # ------------------------------------------------------------------
        aorta_raw = ((arr >= p75) & (arr < p88) & (seg == 0)).astype(np.int32)
        aorta_labeled, n_aorta = ndimage.label(aorta_raw)
        if n_aorta > 0:
            sizes = ndimage.sum(aorta_raw, aorta_labeled, range(1, n_aorta + 1))
            # Aorta is a relatively large structure; pick the largest candidate
            largest_id = int(np.argmax(sizes)) + 1
            # Erode to keep shape tubular
            aorta_mask = (aorta_labeled == largest_id).astype(np.int32)
            aorta_eroded = ndimage.binary_erosion(
                aorta_mask, structure=np.ones((2, 2, 2)), iterations=1
            )
            seg[aorta_eroded] = 4

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

    @staticmethod
    def _generate_processing_notes(
        qa_flags: list[str],
        right_lung_vol: float,
        left_lung_vol: float,
        heart_vol: float,
        lesion_detected: bool,
        bilateral_analysis: bool,
    ) -> str:
        notes = []
        if bilateral_analysis:
            notes.append(
                f"Bilateral lung analysis completed "
                f"(R: {right_lung_vol:.1f} mL, L: {left_lung_vol:.1f} mL)."
            )
        elif right_lung_vol > 0 or left_lung_vol > 0:
            notes.append("Only unilateral lung could be segmented.")
        else:
            notes.append("No lung parenchyma detected in this study.")
        if heart_vol > 0:
            notes.append(f"Heart volume: {heart_vol:.1f} mL.")
        if lesion_detected:
            notes.append(
                "Significant lung volume asymmetry detected; "
                "possible lesion or collapse. Clinical correlation recommended."
            )
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
        if not qa_flags and bilateral_analysis:
            notes.append("Processing completed normally with no quality concerns.")
        return " ".join(notes)
