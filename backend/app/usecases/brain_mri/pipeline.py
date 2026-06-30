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
import torch
import yaml
from monai.inferers import sliding_window_inference
from monai.networks.nets import SegResNet
from monai.transforms import (
    Compose,
    EnsureChannelFirstd,
    LoadImaged,
    NormalizeIntensityd,
    Orientationd,
    Spacingd,
    EnsureTyped,
)
from scipy import ndimage

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.usecases.base import BasePipeline

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

SEQUENCE_PATTERNS = {
    "T1": [
        r"(?i)\bt1\b", r"(?i)mprage", r"(?i)bravo", r"(?i)t1w",
        r"(?i)spgr", r"(?i)3d.*t1", r"(?i)t1.*3d",
    ],
    "T2": [
        r"(?i)\bt2\b", r"(?i)t2w", r"(?i)t2.*fse", r"(?i)t2.*tse",
    ],
    "FLAIR": [
        r"(?i)flair", r"(?i)t2.*flair", r"(?i)dark.*fluid",
    ],
}


def _download_brats_bundle(bundle_dir: str, max_retries: int = 3) -> Path:
    """Download the BraTS MRI segmentation bundle from the MONAI Model Zoo.

    Uses monai.bundle.download which fetches a SegResNet trained on BraTS 2021
    from https://github.com/Project-MONAI/model-zoo.
    """
    import time
    from monai.bundle import download

    bundle_root = Path(bundle_dir)
    bundle_root.mkdir(parents=True, exist_ok=True)

    marker = bundle_root / "brats_mri_segmentation" / ".download_complete"
    if marker.exists():
        logger.info("brats_bundle_already_cached", path=str(bundle_root))
        return bundle_root / "brats_mri_segmentation"

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "downloading_brats_bundle",
                destination=str(bundle_root),
                attempt=attempt,
                max_retries=max_retries,
            )
            download(
                name="brats_mri_segmentation",
                bundle_dir=str(bundle_root),
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok")
            logger.info("brats_bundle_downloaded", path=str(bundle_root))
            return bundle_root / "brats_mri_segmentation"
        except Exception as exc:
            last_error = exc
            logger.warning(
                "brats_bundle_download_failed",
                attempt=attempt,
                error=str(exc),
            )
            if attempt < max_retries:
                time.sleep(5 * attempt)

    raise RuntimeError(
        f"Failed to download BraTS bundle after {max_retries} attempts. "
        f"Ensure the worker container has internet access. Last error: {last_error}"
    )


class Pipeline(BasePipeline):
    """Brain MRI segmentation pipeline using the MONAI BraTS SegResNet bundle.

    The model auto-downloads from the MONAI Model Zoo on first run.
    No manual weight provisioning is required.

    Performs:
    - Sequence identification and validation from DICOM metadata
    - NIfTI conversion via SimpleITK
    - Brain tumor segmentation using SegResNet (BraTS-trained)
    - Volumetric measurements for each segmented structure
    - Quality assurance checks (spacing, motion artifacts, coverage)
    """

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self._config = yaml.safe_load(f)
        self._model = None
        self._device = None
        self._bundle_path: Path | None = None
        self._model_checksum_cache: str | None = None
        self._sw_model: torch.nn.Module | None = None
        self._sw_device: torch.device | None = None
        sw_cfg = self._config.get("swin_unetr", {})
        sw_path = sw_cfg.get("custom_weights_path")
        if sw_path:
            try:
                self._load_swin_unetr(sw_path, sw_cfg)
            except Exception as exc:
                logger.warning("swin_unetr_load_failed", weights=sw_path, error=str(exc))

    def _load_swin_unetr(self, weights_path: str, sw_cfg: dict) -> None:
        from monai.networks.nets import SwinUNETR

        in_channels = sw_cfg.get("in_channels", 4)
        out_channels = sw_cfg.get("out_channels", 4)
        feature_size = sw_cfg.get("feature_size", 48)
        roi_size = tuple(sw_cfg.get("roi_size", [128, 128, 128]))
        use_checkpoint = sw_cfg.get("use_checkpoint", False)
        device_config = self._config["inference"]["device"]
        if device_config == "auto":
            device_config = "cuda" if torch.cuda.is_available() else "cpu"
        self._sw_device = torch.device(device_config)

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

        import hashlib as _hashlib
        sha = _hashlib.sha256()
        with open(weights_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        self._model_checksum_cache = sha.hexdigest()[:16]
        logger.info("swin_unetr_loaded", path=weights_path, device=str(self._sw_device))

    def _run_swin_unetr_brain(
        self, img_tensor: torch.Tensor, sw_cfg: dict
    ) -> np.ndarray:
        roi_size = tuple(sw_cfg.get("roi_size", [128, 128, 128]))
        sw_batch_size = sw_cfg.get("sw_batch_size", 1)
        overlap = sw_cfg.get("overlap", 0.5)
        mode = sw_cfg.get("mode", "gaussian")

        with torch.no_grad():
            output = sliding_window_inference(
                img_tensor.to(self._sw_device), roi_size, sw_batch_size, self._sw_model,
                overlap=overlap, mode=mode,
            )

        # softmax argmax → {0: bg, 1: tumor_core, 2: whole_tumor, 3: enhancing_tumor}
        return torch.argmax(output, dim=1)[0].cpu().numpy().astype(np.uint8)

    def _get_device(self) -> torch.device:
        if self._device is not None:
            return self._device
        device_config = self._config["inference"]["device"]
        if device_config == "auto":
            use_cuda = False
            if torch.cuda.is_available():
                try:
                    torch.cuda.get_device_name(0)  # probe — raises if no real GPU
                    use_cuda = True
                except Exception:
                    pass
            self._device = torch.device("cuda" if use_cuda else "cpu")
        else:
            self._device = torch.device(device_config)
        logger.info("inference_device", device=str(self._device))
        return self._device

    def _ensure_bundle(self) -> Path:
        """Download the MONAI bundle if not already cached."""
        if self._bundle_path is not None:
            return self._bundle_path
        cache_dir = self._config["model"]["bundle_cache_dir"]
        self._bundle_path = _download_brats_bundle(cache_dir)
        return self._bundle_path

    def _load_model(self) -> torch.nn.Module:
        if self._model is not None:
            return self._model

        device = self._get_device()

        # Prefer custom weights if explicitly configured
        custom_path = self._config["model"].get("custom_weights_path")
        if custom_path and Path(custom_path).exists():
            logger.info("loading_custom_weights", path=custom_path)
            model = SegResNet(
                blocks_down=[1, 2, 2, 4],
                blocks_up=[1, 1, 1],
                init_filters=16,
                in_channels=4,
                out_channels=3,
                dropout_prob=0.2,
            )
            state_dict = torch.load(custom_path, map_location="cpu", weights_only=False)
            if "state_dict" in state_dict:
                state_dict = state_dict["state_dict"]
            model.load_state_dict(state_dict, strict=False)
            model = model.to(device)
            model.eval()
            self._model = model
            return model

        # Otherwise, use the auto-downloaded BraTS bundle
        bundle_path = self._ensure_bundle()
        weights_file = None
        for candidate in [
            bundle_path / "models" / "model.pt",
            bundle_path / "models" / "model_final.pt",
            bundle_path / "models" / "best_metric_model.pt",
        ]:
            if candidate.exists():
                weights_file = candidate
                break

        if not weights_file:
            models_dir = bundle_path / "models"
            available = list(models_dir.glob("*")) if models_dir.exists() else []
            raise FileNotFoundError(
                f"No model weights found in {models_dir}. "
                f"Available: {[f.name for f in available]}"
            )

        logger.info("loading_brats_bundle_weights", path=str(weights_file))

        model = SegResNet(
            blocks_down=[1, 2, 2, 4],
            blocks_up=[1, 1, 1],
            init_filters=16,
            in_channels=4,
            out_channels=3,
            dropout_prob=0.2,
        )

        # Always load to CPU first; .to(device) below moves to GPU if available.
        # Passing torch.device('cpu') object to map_location fails in PyTorch 2.7
        # when the weights file was saved on CUDA — use the 'cpu' string instead.
        state_dict = torch.load(str(weights_file), map_location="cpu", weights_only=False)
        if "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
        if "model" in state_dict:
            state_dict = state_dict["model"]
        model.load_state_dict(state_dict, strict=False)

        model = model.to(device)
        model.eval()
        self._model = model

        logger.info("brats_model_loaded", device=str(device), weights=str(weights_file))
        return model

    def _get_model_checksum(self) -> str:
        if self._model_checksum_cache:
            return self._model_checksum_cache

        custom_path = self._config["model"].get("custom_weights_path")
        if custom_path and Path(custom_path).exists():
            target = Path(custom_path)
        else:
            bundle_path = self._ensure_bundle()
            target = None
            for c in [
                bundle_path / "models" / "model.pt",
                bundle_path / "models" / "model_final.pt",
                bundle_path / "models" / "best_metric_model.pt",
            ]:
                if c.exists():
                    target = c
                    break

        if target and target.exists():
            sha = hashlib.sha256()
            with open(target, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha.update(chunk)
            self._model_checksum_cache = sha.hexdigest()[:16]
        else:
            self._model_checksum_cache = "unknown"

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
            "brain_mri_preprocess_start",
            study_uid=study.study_instance_uid,
            series_count=len(series),
        )

        classified = self._classify_sequences(series)
        qa_flags = []
        qa_details = {}

        if "T1" not in classified and "FLAIR" not in classified:
            qa_flags.append("missing_sequence")
            qa_details["missing_sequences"] = [
                s for s in ["T1", "FLAIR"] if s not in classified
            ]
            logger.warning("missing_required_sequences", classified=list(classified.keys()))

        # BraTS model expects 4 channels: T1, T1ce, T2, FLAIR.
        # Download every classified sequence, then replicate the primary
        # into any missing channels.
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

        # QA on the first available volume
        first_nifti = next(iter(downloaded_niftis.values()))
        spacing_qa = self._check_spacing(first_nifti)
        qa_flags.extend(spacing_qa.get("flags", []))
        qa_details.update(spacing_qa.get("details", {}))

        motion_qa = self._check_motion_artifacts(first_nifti)
        qa_flags.extend(motion_qa.get("flags", []))
        qa_details.update(motion_qa.get("details", {}))

        # Determine best available sequence for each BraTS channel
        primary_seq = next(
            (p for p in ["T1", "FLAIR", "T2", "FALLBACK"] if p in downloaded_niftis),
            next(iter(downloaded_niftis)),
        )
        channel_order = ["T1", "T1", "T2", "FLAIR"]  # T1ce ≈ T1 for non-contrast
        channel_paths = []
        sequences_used = []
        for ch_name in channel_order:
            if ch_name in downloaded_niftis:
                channel_paths.append(downloaded_niftis[ch_name])
                sequences_used.append(ch_name)
            else:
                channel_paths.append(downloaded_niftis[primary_seq])
                sequences_used.append(f"{primary_seq}(as_{ch_name})")

        # Build 4-channel NIfTI
        preprocessed_dir = os.path.join(working_dir, "preprocessed")
        os.makedirs(preprocessed_dir, exist_ok=True)
        multichannel_path = os.path.join(preprocessed_dir, "input_4ch.nii.gz")
        self._build_multichannel_input(channel_paths, multichannel_path)

        logger.info(
            "brain_mri_preprocess_complete",
            sequences_used=sequences_used,
            qa_flags=qa_flags,
        )

        return {
            "input_path": multichannel_path,
            "original_nifti_path": first_nifti,
            "primary_sequence": primary_seq,
            "sequences_used": sequences_used,
            "classified_sequences": {k: v.series_instance_uid for k, v in classified.items()},
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("brain_mri_inference_start")

        img_nib = nib.load(preprocessed["input_path"])
        img_data = img_nib.get_fdata().astype(np.float32)
        affine = img_nib.affine

        # (H, W, D, 4) → (1, 4, H, W, D) for the model
        if img_data.ndim == 4:
            img_tensor = torch.from_numpy(img_data).permute(3, 0, 1, 2).unsqueeze(0)
        elif img_data.ndim == 3:
            img_tensor = torch.from_numpy(img_data).unsqueeze(0).unsqueeze(0).repeat(1, 4, 1, 1, 1)
        else:
            raise ValueError(f"Unexpected input shape: {img_data.shape}")

        sw_cfg = self._config.get("swin_unetr", {})
        seg_array = None
        inference_method = None

        if self._sw_model is not None:
            try:
                seg_array = self._run_swin_unetr_brain(img_tensor, sw_cfg)
                inference_method = "swin_unetr"
            except Exception as exc:
                logger.warning("swin_unetr_inference_failed_falling_back", error=str(exc))

        if seg_array is None:
            device = self._get_device()
            model = self._load_model()
            img_tensor = img_tensor.to(device)

            roi_size = tuple(self._config["inference"]["sliding_window"]["roi_size"])
            sw_batch_size = self._config["inference"]["sliding_window"]["sw_batch_size"]
            overlap = self._config["inference"]["sliding_window"]["overlap"]

            logger.info(
                "running_segresnet_inference",
                input_shape=list(img_tensor.shape),
                roi_size=roi_size,
                device=str(device),
            )

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

            # BraTS SegResNet: sigmoid multi-label → priority merge ET>TC>WT>bg
            output_sigmoid = torch.sigmoid(output)
            output_np = output_sigmoid[0].cpu().numpy()  # (3, H, W, D)
            threshold = 0.5
            tc_mask = output_np[0] > threshold
            wt_mask = output_np[1] > threshold
            et_mask = output_np[2] > threshold
            seg_array = np.zeros(output_np.shape[1:], dtype=np.uint8)
            seg_array[wt_mask] = 2
            seg_array[tc_mask] = 1
            seg_array[et_mask] = 3
            inference_method = "segresnet" if inference_method is None else "segresnet_fallback"

        seg_path = os.path.join(working_dir, "segmentation_raw.nii.gz")
        seg_img = nib.Nifti1Image(seg_array, affine=affine)
        nib.save(seg_img, seg_path)

        logger.info(
            "brain_mri_inference_complete",
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
            **preprocessed,
        }

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("brain_mri_postprocess_start")

        seg_array = inference_output["segmentation_array"]
        affine = inference_output["affine"]
        label_map = self._config["postprocessing"]["label_map"]
        min_lesion_vol = self._config["postprocessing"].get("min_lesion_volume_ml", 0.05)

        if isinstance(affine, np.ndarray):
            voxel_spacing = np.abs(np.diag(affine[:3, :3]))
        else:
            voxel_spacing = np.array([1.0, 1.0, 1.0])
        voxel_volume_ml = float(np.prod(voxel_spacing)) / 1000.0

        seg_clean = seg_array.copy()

        if self._config["postprocessing"].get("apply_connected_components", False):
            seg_clean = self._apply_connected_components(
                seg_clean, label_map,
                self._config["postprocessing"].get("largest_component_only_labels", []),
            )

        # Remove small tumor regions below the volume threshold — driven by label_map
        non_bg_labels = sorted(int(k) for k in label_map if int(k) != 0)
        for label_id in non_bg_labels:
            if label_id not in np.unique(seg_clean):
                continue
            label_mask = (seg_clean == label_id).astype(np.int32)
            labeled, num_features = ndimage.label(label_mask)
            for comp_id in range(1, num_features + 1):
                comp_volume = float(np.sum(labeled == comp_id)) * voxel_volume_ml
                if comp_volume < min_lesion_vol:
                    seg_clean[labeled == comp_id] = 0

        # Volumetric measurements
        volumes_ml = {}
        volumes_percent = {}
        total_nonzero_voxels = 0
        for label_id_str, label_name in label_map.items():
            label_id = int(label_id_str)
            if label_id == 0:
                continue
            count = int(np.sum(seg_clean == label_id))
            vol_ml = count * voxel_volume_ml
            volumes_ml[label_name] = round(vol_ml, 2)
            total_nonzero_voxels += count

        total_lesion_volume_ml = total_nonzero_voxels * voxel_volume_ml
        for label_name, vol in volumes_ml.items():
            if total_lesion_volume_ml > 0:
                volumes_percent[label_name] = round((vol / total_lesion_volume_ml) * 100, 1)
            else:
                volumes_percent[label_name] = 0.0

        # Save artifacts
        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        seg_nifti_path = os.path.join(artifacts_dir, "segmentation.nii.gz")
        seg_img = nib.Nifti1Image(seg_clean.astype(np.uint8), affine=affine)
        nib.save(seg_img, seg_nifti_path)

        tumor_detected = total_lesion_volume_ml > 0

        # Tier 1/2 enrichment — derive lesion geometry (size/location/count) and
        # relative signal characterisation from the actual segmentation mask +
        # registered sequences. Best-effort: never let it crash the pipeline.
        lesion_geometry: dict[str, Any] = {}
        signal_profile: dict[str, str] = {}
        if tumor_detected:
            try:
                lesion_geometry = self._compute_lesion_geometry(
                    seg_clean, affine, voxel_spacing, voxel_volume_ml, min_lesion_vol
                )
            except Exception as exc:
                logger.warning("lesion_geometry_failed", error=str(exc))
            try:
                signal_profile = self._compute_signal_profile(
                    inference_output.get("input_path"),
                    seg_clean,
                    inference_output.get("sequences_used", []),
                )
            except Exception as exc:
                logger.warning("signal_profile_failed", error=str(exc))

        report = {
            "summary": {
                "tumor_detected": tumor_detected,
                "total_lesion_volume_ml": round(total_lesion_volume_ml, 2),
                "segmentation_labels": {
                    str(k): v for k, v in label_map.items() if int(k) != 0
                },
                "sequences_used": inference_output.get("sequences_used", []),
                "inference_method": inference_output.get("inference_method", "unknown"),
                "signal_profile": signal_profile,
                **lesion_geometry,
                "processing_notes": self._generate_processing_notes(
                    inference_output.get("qa_flags", []),
                    volumes_ml,
                    tumor_detected,
                ),
            },
            "measurements": {
                "volumes_ml": volumes_ml,
                "volumes_percent": volumes_percent,
                "voxel_spacing_mm": [round(float(s), 3) for s in voxel_spacing],
                "image_dimensions": inference_output.get("image_shape", []),
            },
        }

        report_path = os.path.join(artifacts_dir, "report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        inference_method = inference_output.get("inference_method", "")
        model_version = self._config["model"].get("version", "1.0.0")
        model_checksum = self._get_model_checksum()

        if inference_method and inference_method.startswith("swin_unetr"):
            sw_path = self._config.get("swin_unetr", {}).get("custom_weights_path", "unknown")
            model_version_str = f"brain_mri_swinunetr_{Path(sw_path).stem}"
        else:
            model_version_str = f"brats_segresnet_v{model_version}"

        qa_flags = inference_output.get("qa_flags", [])
        qa_details = inference_output.get("qa_details", {})
        qa_details["segmentation_stats"] = {
            "unique_labels": [int(x) for x in np.unique(seg_clean).tolist()],
            "total_lesion_volume_ml": round(total_lesion_volume_ml, 2),
            "voxel_volume_ml": round(voxel_volume_ml, 6),
            "tumor_detected": tumor_detected,
        }

        # Generate preview overlay images (segmentation on MRI slices)
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
                    target_size=1024,
                )
                logger.info(
                    "preview_images_generated", count=len(preview_artifacts)
                )
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
            "brain_mri_postprocess_complete",
            total_lesion_volume_ml=round(total_lesion_volume_ml, 2),
            structures_found=len([v for v in volumes_ml.values() if v > 0]),
            tumor_detected=tumor_detected,
            qa_flags=qa_flags,
        )

        return result

    # =====================================================================
    # Helpers
    # =====================================================================

    @staticmethod
    def _extract_brain_mask(arr: np.ndarray) -> np.ndarray:
        """Heuristic brain extraction (skull-strip) for a single 3-D volume.

        Otsu-thresholds the head from background air, then severs the thin
        skull/scalp connection with an erosion, keeps the largest connected
        component (the brain), and dilates back with hole-filling. The dilation
        matches the erosion so surface / extra-axial tissue (e.g. meningioma
        against the skull) is preserved rather than shaved off.

        This is a dependency-free approximation; a dedicated brain-extraction
        model (HD-BET / SynthStrip) is the robust upgrade. Returns a float32
        mask (1.0 inside brain, 0.0 outside) the same shape as ``arr``.
        """
        from scipy import ndimage

        if not np.any(arr > 0):
            return np.ones_like(arr, dtype=np.float32)

        # Otsu split: head (foreground) vs surrounding air.
        sitk_img = sitk.GetImageFromArray(arr.astype(np.float32))
        otsu = sitk.OtsuThreshold(sitk_img, 0, 1)  # inside (head) -> 1
        head = sitk.GetArrayFromImage(otsu).astype(bool)
        if not head.any():
            return np.ones_like(arr, dtype=np.float32)

        struct = ndimage.generate_binary_structure(3, 1)
        iters = 5  # ~5 mm at 1 mm spacing — enough to sever skull/scalp

        eroded = ndimage.binary_erosion(head, structure=struct, iterations=iters)
        labeled, n = ndimage.label(eroded)
        if n == 0:
            return head.astype(np.float32)  # erosion removed everything — keep head

        sizes = ndimage.sum(np.ones_like(labeled, dtype=np.float32), labeled, range(1, n + 1))
        largest = int(np.argmax(np.atleast_1d(sizes))) + 1
        brain = labeled == largest

        brain = ndimage.binary_dilation(brain, structure=struct, iterations=iters)
        brain = ndimage.binary_fill_holes(brain)
        brain &= head  # never extend beyond the head boundary
        return brain.astype(np.float32)

    def _build_multichannel_input(self, channel_paths: list[str], output_path: str):
        """Stack 4 NIfTI volumes into a single 4-channel NIfTI for BraTS input.

        Each channel is resampled to the same grid, skull-stripped (optional),
        and z-score normalized.
        """
        target_spacing = self._config["preprocessing"]["target_spacing"]
        # BraTS is trained on skull-stripped brains; stripping the skull/scalp brings
        # our input in-distribution and stops the model labelling bright bone/scalp as
        # tumour. Disable via inference_config preprocessing.skull_strip: false.
        skull_strip = self._config["preprocessing"].get("skull_strip", True)
        ref_img = sitk.ReadImage(channel_paths[0])

        channels = []
        for path in channel_paths:
            img = sitk.ReadImage(path)
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
            if skull_strip:
                try:
                    arr = arr * self._extract_brain_mask(arr)
                except Exception as exc:
                    logger.warning("skull_strip_failed", error=str(exc))
            nonzero_mask = arr > 0
            if np.sum(nonzero_mask) > 0:
                mean_val = float(np.mean(arr[nonzero_mask]))
                std_val = float(np.std(arr[nonzero_mask]))
                if std_val > 0:
                    arr = (arr - mean_val) / std_val
                    arr[~nonzero_mask] = 0
            channels.append(arr)

        min_shape = np.min([ch.shape for ch in channels], axis=0)
        cropped = [ch[tuple(slice(0, s) for s in min_shape)] for ch in channels]
        stacked = np.stack(cropped, axis=-1)  # (D, H, W, 4)

        direction = np.array(ref_img.GetDirection()).reshape(3, 3)
        spacing_arr = np.array(target_spacing)
        origin = np.array(ref_img.GetOrigin())
        affine = np.eye(4)
        affine[:3, :3] = direction * spacing_arr
        affine[:3, 3] = origin

        nib_img = nib.Nifti1Image(stacked, affine=affine)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        nib.save(nib_img, output_path)

        logger.info("multichannel_input_built", shape=list(stacked.shape))

    def _classify_sequences(self, series: list[Series]) -> dict[str, Series]:
        classified = {}
        for s in series:
            desc = (s.series_description or "").strip()
            protocol = (s.protocol_name or "").strip() if hasattr(s, "protocol_name") else ""
            combined = f"{desc} {protocol}"
            tags = s.dicom_tags or {}
            inversion_time = tags.get("InversionTime")

            for seq_name, patterns in SEQUENCE_PATTERNS.items():
                if seq_name in classified:
                    continue
                for pat in patterns:
                    if re.search(pat, combined):
                        classified[seq_name] = s
                        break

            if "FLAIR" not in classified and inversion_time:
                try:
                    if float(inversion_time) > 1500:
                        classified["FLAIR"] = s
                except (ValueError, TypeError):
                    pass

        logger.info("sequence_classification", result={k: v.series_description for k, v in classified.items()})
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
            details["coverage_issue"] = f"Min dimension {min(size)} < threshold {qc['min_slices']}"

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

        details["coefficient_of_variation"] = round(float(np.std(roi_flat)) / (mean_signal + 1e-8), 4)
        details["normalized_edge_energy"] = round(normalized_edge, 4)

        if normalized_edge > self._config["quality_checks"]["motion_artifact_threshold"]:
            flags.append("motion_artifact")
            details["motion_assessment"] = "Elevated edge energy suggesting possible motion"

        return {"flags": flags, "details": details}

    @staticmethod
    def _apply_connected_components(
        seg: np.ndarray, label_map: dict, largest_only_labels: list[int],
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
            largest = np.argmax(sizes) + 1
            result[np.logical_and(labeled != largest, labeled > 0)] = 0
        return result

    @staticmethod
    def _generate_processing_notes(
        qa_flags: list[str], volumes: dict[str, float], tumor_detected: bool,
    ) -> str:
        notes = []
        if tumor_detected:
            total = sum(volumes.values())
            notes.append(f"Segmentation detected {total:.2f} mL total lesion volume.")
        else:
            notes.append("No tumor regions detected in this study.")
        if "missing_sequence" in qa_flags:
            notes.append("Some expected sequences were missing; available channels were replicated.")
        if "motion_artifact" in qa_flags:
            notes.append("Possible motion artifacts detected; review segmentation carefully.")
        if "spacing_inconsistency" in qa_flags:
            notes.append("Unusual voxel spacing detected.")
        if not qa_flags and tumor_detected:
            notes.append("Processing completed normally with no quality concerns.")
        return " ".join(notes)

    @staticmethod
    def _compute_lesion_geometry(
        seg_clean: np.ndarray,
        affine: Any,
        voxel_spacing: np.ndarray,
        voxel_volume_ml: float,
        min_lesion_vol: float,
    ) -> dict[str, Any]:
        """Derive lesion size (AP×TS×CC cm), hemisphere, and lesion count from the
        segmentation mask. All quantities come from the model's actual output —
        nothing is inferred beyond the geometry of the mask.
        """
        lesion_mask = seg_clean > 0
        if not lesion_mask.any():
            return {}

        labeled, num = ndimage.label(lesion_mask)
        if num == 0:
            return {}

        comp_voxels = ndimage.sum(
            np.ones_like(labeled, dtype=np.float32), labeled, range(1, num + 1)
        )
        comp_voxels = np.atleast_1d(comp_voxels)
        significant = int(sum(1 for c in comp_voxels if c * voxel_volume_ml >= min_lesion_vol))
        lesion_count = significant if significant > 0 else int(num)

        # Largest component drives dimensions / location.
        largest = int(np.argmax(comp_voxels)) + 1
        coords = np.array(np.where(labeled == largest))  # (3, N)
        mins = coords.min(axis=1)
        maxs = coords.max(axis=1)
        extent_vox = (maxs - mins + 1).astype(np.float64)
        dims_mm = extent_vox * np.asarray(voxel_spacing, dtype=np.float64)[: extent_vox.shape[0]]

        result: dict[str, Any] = {"lesion_count": lesion_count}

        # Map array axes → anatomical axes via the affine orientation codes.
        try:
            axcodes = nib.aff2axcodes(affine) if isinstance(affine, np.ndarray) else None
        except Exception:
            axcodes = None

        dims_cm: dict[str, float] = {}
        location: str | None = None
        if axcodes and len(axcodes) >= 3:
            centroid = coords.mean(axis=1)
            for ax, code in enumerate(axcodes[:3]):
                if ax >= dims_mm.shape[0]:
                    continue
                cm = round(float(dims_mm[ax]) / 10.0, 1)
                if code in ("L", "R"):
                    dims_cm["transverse"] = cm
                    mid = seg_clean.shape[ax] / 2.0
                    c = centroid[ax]
                    if abs(c - mid) < 0.05 * seg_clean.shape[ax]:
                        location = "midline"
                    elif (code == "R") == (c > mid):
                        location = "right"
                    else:
                        location = "left"
                elif code in ("A", "P"):
                    dims_cm["ap"] = cm
                elif code in ("S", "I"):
                    dims_cm["craniocaudal"] = cm
        else:
            # No orientation info — report raw extents without anatomical labels.
            dims_cm = {
                k: round(float(dims_mm[i]) / 10.0, 1)
                for i, k in enumerate(("dim1", "dim2", "dim3"))
                if i < dims_mm.shape[0]
            }

        if dims_cm:
            result["lesion_dimensions_cm"] = dims_cm
        if location:
            result["lesion_location"] = location
        return result

    @staticmethod
    def _compute_signal_profile(
        input_path: str | None,
        seg_clean: np.ndarray,
        sequences_used: list[str],
    ) -> dict[str, str]:
        """Characterise lesion signal (hyper/hypo/iso-intense) per genuine sequence.

        The 4-channel input is z-score normalised (brain mean ≈ 0, std ≈ 1), so the
        mean intensity inside the lesion mask, measured in std units, gives signal
        relative to surrounding brain parenchyma. Channels that were replicated as a
        fallback (e.g. "T1(as_T2)") are skipped — that signal is not the real modality.
        """
        if not input_path or not os.path.exists(input_path):
            return {}
        img = nib.load(input_path).get_fdata().astype(np.float32)
        if img.ndim != 4:
            return {}

        mask = seg_clean > 0
        if not mask.any():
            return {}

        sh = tuple(min(a, b) for a, b in zip(mask.shape, img.shape[:3]))
        m = mask[: sh[0], : sh[1], : sh[2]]

        intended = ["T1", "T1ce", "T2", "FLAIR"]
        profile: dict[str, str] = {}
        n = min(img.shape[3], len(sequences_used), len(intended))
        for idx in range(n):
            used = sequences_used[idx]
            if "(as_" in used:  # replicated fallback — not the genuine modality
                continue
            modality = intended[idx]
            if modality == "T1ce":  # duplicate of T1 channel for non-contrast input
                continue
            chan = img[: sh[0], : sh[1], : sh[2], idx]
            vals = chan[m]
            if vals.size < 10:
                continue
            mean_in = float(np.mean(vals))
            if mean_in > 0.4:
                profile[modality] = "hyperintense"
            elif mean_in < -0.4:
                profile[modality] = "hypointense"
            else:
                profile[modality] = "isointense"
        return profile
