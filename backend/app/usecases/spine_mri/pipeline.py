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

# Spine MRI sequence classification patterns.
# Keys must match the names used in CHANNEL_ORDER and preprocess logic.
SEQUENCE_PATTERNS = {
    "SAG_T2": [
        r"(?i)sag.*t2",
        r"(?i)t2.*sag",
        r"(?i)sagittal.*t2",
        r"(?i)t2.*sagittal",
        r"(?i)sag.*tse",
        r"(?i)sag.*fse",
    ],
    "AX_T2": [
        r"(?i)ax.*t2",
        r"(?i)t2.*ax",
        r"(?i)axial.*t2",
        r"(?i)t2.*axial",
        r"(?i)ax.*tse",
        r"(?i)ax.*fse",
        r"(?i)tra.*t2",
    ],
    "T1_SAG": [
        r"(?i)sag.*t1",
        r"(?i)t1.*sag",
        r"(?i)sagittal.*t1",
        r"(?i)t1.*sagittal",
        r"(?i)sag.*se",
    ],
}

# Vertebral level labels used for summary output.
_CERVICAL_LEVELS = ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]
_THORACIC_LEVELS = [f"T{i}" for i in range(1, 13)]
_LUMBAR_LEVELS = ["L1", "L2", "L3", "L4", "L5"]
_SACRAL_LEVELS = ["S1"]
_ALL_LEVELS = _CERVICAL_LEVELS + _THORACIC_LEVELS + _LUMBAR_LEVELS + _SACRAL_LEVELS


class Pipeline(BasePipeline):
    """Spine MRI segmentation pipeline.

    Segments four structures: vertebrae, intervertebral discs, spinal canal,
    and spinal cord.  When no trained model weights are available the pipeline
    falls back to synthetic inference (intensity-thresholding + connected
    components) so that the platform can exercise the full three-phase
    contract without requiring GPU resources or downloaded bundles.

    Performs:
    - Sequence classification from DICOM series descriptions
    - NIfTI download and multi-sequence stacking
    - QA checks for spacing, coverage, and motion artifacts
    - Segmentation (real model or synthetic fallback)
    - Volumetric measurements: disc count, canal/cord cross-sectional areas,
      vertebra count
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
        self._spinenet = None  # lazily loaded (heavy: 4 sub-models)
        sw_cfg = self._config.get("swin_unetr", {})
        sw_path = sw_cfg.get("custom_weights_path")
        if sw_path:
            try:
                self._load_swin_unetr(sw_path, sw_cfg)
            except Exception as exc:
                logger.warning("swin_unetr_load_failed", weights=sw_path, error=str(exc))

    # =====================================================================
    # SpineNet (vertebral level labelling + disc grading) — hybrid add-on
    # =====================================================================

    @staticmethod
    def _ensure_spinenet_importable() -> None:
        """Make the `spinenet` package importable even if not pip-installed.

        The submodule lives at backend/external/spinenet (inside the Docker build
        context, so it is baked into the image and also present via the
        ./backend:/app dev mount). Resolution order:
          1. already importable (pip install -e backend/external/spinenet) → done
          2. SPINENET_PATH env var (optional explicit override)
          3. backend-relative external/spinenet (image + host / non-Docker runs)
        """
        import os
        import sys

        try:
            import spinenet  # noqa: F401
            return
        except Exception:
            pass

        candidates: list[Path] = []
        env_path = os.environ.get("SPINENET_PATH")
        if env_path:
            candidates.append(Path(env_path))
        # backend/app/usecases/spine_mri/pipeline.py → backend root is parents[3]
        candidates.append(Path(__file__).resolve().parents[3] / "external" / "spinenet")

        for candidate in candidates:
            if candidate.exists() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))

    def _get_spinenet(self):
        """Lazily construct the SpineNet model (cached). Returns None when
        disabled or unavailable so callers fall back to TotalSegmentator levels.
        """
        if self._spinenet is not None:
            return self._spinenet
        cfg = self._config.get("spinenet", {})
        if not cfg.get("enabled", False):
            return None
        try:
            import torch

            self._ensure_spinenet_importable()
            from spinenet import SpineNet, download_weights

            if cfg.get("auto_download_weights", True):
                try:
                    download_weights(verbose=False)
                except Exception as exc:
                    logger.warning("spinenet_weight_download_failed", error=str(exc))

            device = cfg.get("device", "auto")
            if device == "auto":
                device = "cuda:0" if torch.cuda.is_available() else "cpu"
            self._spinenet = SpineNet(
                device=device,
                verbose=False,
                scan_type=cfg.get("scan_type", "lumbar"),
            )
            logger.info("spinenet_loaded", device=device, scan_type=cfg.get("scan_type", "lumbar"))
            return self._spinenet
        except Exception as exc:
            logger.warning("spinenet_load_failed", error=str(exc))
            return None

    @staticmethod
    def _order_spinenet_levels(levels: list[str]) -> list[str]:
        """Order SpineNet vertebra labels superior→inferior (handles S2)."""
        rank = {lv: i for i, lv in enumerate(_ALL_LEVELS + ["S2"])}
        seen = list(dict.fromkeys(levels))  # de-dup, preserve first occurrence
        return sorted(seen, key=lambda lv: rank.get(lv, 999))

    def _run_spinenet(self, dicom_dir: str) -> dict[str, Any] | None:
        """Run SpineNet on a folder of sagittal T2 DICOMs.

        Returns ``{"levels", "disc_gradings", "vertebra_count"}`` or None on any
        failure (caller then falls back to TotalSegmentator-derived levels).
        """
        sn = self._get_spinenet()
        if sn is None:
            return None
        cfg = self._config.get("spinenet", {})
        try:
            from spinenet.io import load_dicoms_from_folder

            scan = load_dicoms_from_folder(
                dicom_dir, require_extensions=cfg.get("require_dcm_extension", False)
            )
            vert_dicts = sn.detect_vb(scan.volume, scan.pixel_spacing)
            levels = [vd.get("predicted_label") for vd in vert_dicts if vd.get("predicted_label")]
            ordered = self._order_spinenet_levels(levels)

            disc_gradings: list[dict[str, Any]] = []
            try:
                ivd_dicts = sn.get_ivds_from_vert_dicts(vert_dicts, scan.volume)
                df = sn.grade_ivds(ivd_dicts)
                for level_name, row in df.iterrows():
                    entry: dict[str, Any] = {"level": str(level_name)}
                    for col, val in row.items():
                        entry[str(col).lower()] = int(val)
                    disc_gradings.append(entry)
            except Exception as exc:
                logger.warning("spinenet_grading_failed", error=str(exc))

            logger.info(
                "spinenet_inference_complete",
                levels=ordered,
                disc_count=len(disc_gradings),
            )
            return {
                "levels": ordered,
                "disc_gradings": disc_gradings,
                "vertebra_count": len(ordered),
            }
        except Exception as exc:
            logger.warning("spinenet_inference_failed", error=str(exc))
            return None

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
            # torch unavailable — will fall back to synthetic inference
            self._device = None
        logger.info("inference_device", device=str(self._device))
        return self._device

    def _load_model(self):
        """Attempt to load a real segmentation model from custom_weights_path.

        Returns the model on success, or None when no weights are available
        (triggering the synthetic inference fallback).
        """
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
            logger.info("spine_model_loaded", path=custom_path, device=str(device))
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
            "spine_mri_preprocess_start",
            study_uid=study.study_instance_uid,
            series_count=len(series),
        )

        classified = self._classify_sequences(series)
        qa_flags = []
        qa_details = {}

        if "SAG_T2" not in classified:
            qa_flags.append("missing_sequence")
            qa_details["missing_sequences"] = [
                s for s in ["SAG_T2"] if s not in classified
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

        # QA on the primary volume
        primary_seq = next(
            (p for p in ["SAG_T2", "AX_T2", "T1_SAG", "FALLBACK"] if p in downloaded_niftis),
            next(iter(downloaded_niftis)),
        )
        first_nifti = downloaded_niftis[primary_seq]

        spacing_qa = self._check_spacing(first_nifti)
        qa_flags.extend(spacing_qa.get("flags", []))
        qa_details.update(spacing_qa.get("details", {}))

        motion_qa = self._check_motion_artifacts(first_nifti)
        qa_flags.extend(motion_qa.get("flags", []))
        qa_details.update(motion_qa.get("details", {}))

        # Build single-channel preprocessed input from primary sequence.
        # For spine, we use the sagittal T2 (or best available) as the main
        # channel; axial T2 is kept as a supplementary reference only.
        preprocessed_dir = os.path.join(working_dir, "preprocessed")
        os.makedirs(preprocessed_dir, exist_ok=True)
        input_path = os.path.join(preprocessed_dir, "input_1ch.nii.gz")
        self._build_single_channel_input(downloaded_niftis[primary_seq], input_path)

        sequences_used = list(downloaded_niftis.keys())

        # SpineNet needs the sagittal T2 DICOMs (its native, orientation-checked
        # loader). Download them only when SpineNet is enabled and a SAG_T2 series
        # was classified; otherwise SpineNet is skipped and TotalSegmentator levels
        # are used.
        spinenet_dicom_dir: str | None = None
        if self._config.get("spinenet", {}).get("enabled", False) and "SAG_T2" in classified:
            try:
                spinenet_dicom_dir = os.path.join(working_dir, "spinenet_dicoms")
                os.makedirs(spinenet_dicom_dir, exist_ok=True)
                loop.run_until_complete(
                    pacs.download_series_dicoms(
                        study.study_instance_uid,
                        classified["SAG_T2"].series_instance_uid,
                        spinenet_dicom_dir,
                    )
                )
            except Exception as exc:
                logger.warning("spinenet_dicom_download_failed", error=str(exc))
                spinenet_dicom_dir = None

        logger.info(
            "spine_mri_preprocess_complete",
            primary_sequence=primary_seq,
            sequences_used=sequences_used,
            spinenet_dicoms=spinenet_dicom_dir is not None,
            qa_flags=qa_flags,
        )

        return {
            "input_path": input_path,
            "original_nifti_path": first_nifti,
            "primary_sequence": primary_seq,
            "sequences_used": sequences_used,
            "classified_sequences": {k: v.series_instance_uid for k, v in classified.items()},
            "spinenet_dicom_dir": spinenet_dicom_dir,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    def _run_totalsegmentator(
        self, input_path: str, working_dir: str
    ) -> tuple[np.ndarray, list[str]]:
        """Run TotalSegmentator MRI inference for the spine label map.

        Returns ``(seg_array, detected_levels)`` where ``seg_array`` is the merged
        label map (all vertebrae → label 1, as before, so every downstream
        volumetric/measurement/artifact path is unchanged) and ``detected_levels``
        is the list of actual vertebral levels TotalSegmentator identified
        (e.g. ``["L3", "L4", "L5"]``), ordered superior→inferior.

        Previously the per-vertebra level identity that TotalSegmentator produces
        was discarded at merge time and ``levels_analyzed`` was reconstructed from
        a disc-*count* heuristic. We now preserve the true identities so the
        report names the exact levels imaged without losing general anatomical
        context (the merged mask is retained verbatim).
        """
        from totalsegmentator.python_api import totalsegmentator as ts_run

        cfg_model = self._config["model"]
        task = cfg_model.get("totalseg_task", "total_mr")
        weights_dir = cfg_model.get("totalseg_weights_dir", "/model_cache/totalsegmentator")
        organ_map: dict = cfg_model.get("organ_map", {})
        vertebra_prefixes: list = cfg_model.get("vertebra_prefixes", [
            "vertebrae_C", "vertebrae_T", "vertebrae_L", "vertebrae_S"
        ])

        import torch
        import glob as glob_mod
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

        # Merge all vertebra files into label 1, while RECORDING the level
        # identity of each non-empty vertebra (parsed from the filename, e.g.
        # "vertebrae_L4.nii.gz" → "L4"). The merged mask keeps the existing
        # single-label contract; the identities are returned separately.
        vertebra_files = []
        for prefix in vertebra_prefixes:
            vertebra_files.extend(
                glob_mod.glob(os.path.join(ts_out, f"{prefix}*.nii.gz"))
            )
        detected_levels: set[str] = set()
        for vf in vertebra_files:
            mask = nib.load(vf).get_fdata() > 0.5
            if not mask.any():
                continue
            seg_array[mask] = 1
            level = self._parse_vertebra_level(os.path.basename(vf))
            if level:
                detected_levels.add(level)
            logger.info("vertebra_merged", file=os.path.basename(vf), level=level)

        # Order superior→inferior using the canonical level list.
        ordered_levels = self._order_levels(detected_levels)

        # Map remaining organs (spinal_cord → label 4, etc.)
        for organ_name, label_id in organ_map.items():
            organ_path = os.path.join(ts_out, f"{organ_name}.nii.gz")
            if os.path.exists(organ_path):
                mask = nib.load(organ_path).get_fdata() > 0.5
                seg_array[mask] = int(label_id)
            else:
                logger.warning("totalseg_organ_file_missing", organ=organ_name, path=organ_path)

        # Attempt intervertebral disc segmentation (label 2) from ivd files if present
        ivd_files = glob_mod.glob(os.path.join(ts_out, "intervertebral_disc*.nii.gz"))
        for ivd_file in ivd_files:
            mask = nib.load(ivd_file).get_fdata() > 0.5
            seg_array[mask] = 2

        # Derive spinal canal (label 3) as vertebral canal bounding the cord
        spinal_cord_path = os.path.join(ts_out, "spinal_cord.nii.gz")
        spinal_canal_path = os.path.join(ts_out, "spinal_canal.nii.gz")
        if os.path.exists(spinal_canal_path):
            mask = nib.load(spinal_canal_path).get_fdata() > 0.5
            seg_array[mask] = 3
        elif os.path.exists(spinal_cord_path):
            # Estimate canal as dilated cord region not overlapping vertebra
            cord_mask = nib.load(spinal_cord_path).get_fdata() > 0.5
            canal_estimate = ndimage.binary_dilation(cord_mask, iterations=3)
            canal_estimate = canal_estimate & (seg_array == 0)
            seg_array[canal_estimate] = 3

        logger.info(
            "totalsegmentator_complete",
            labels=np.unique(seg_array).tolist(),
            detected_levels=ordered_levels,
        )
        return seg_array, ordered_levels

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("spine_mri_inference_start")

        img_nib = nib.load(preprocessed["input_path"])
        affine = img_nib.affine

        qa_flags = list(preprocessed.get("qa_flags", []))
        architecture = self._config["model"].get("architecture", "segresnet")
        sw_cfg = self._config.get("swin_unetr", {})

        seg_array = None
        inference_method = None
        detected_levels: list[str] = []

        if self._sw_model is not None:
            try:
                img_data = img_nib.get_fdata().astype(np.float32)
                seg_array = self._run_swin_unetr(img_data, sw_cfg)
                inference_method = "swin_unetr"
            except Exception as exc:
                logger.warning("swin_unetr_inference_failed_falling_back", error=str(exc))

        if seg_array is None:
            if architecture == "totalsegmentator_mr":
                seg_array, detected_levels = self._run_totalsegmentator(
                    preprocessed["input_path"], working_dir
                )
                if not np.any(seg_array == 1):
                    logger.warning("spine_vertebrae_empty", detail="No vertebra files matched glob — check totalseg output naming")
                    qa_flags.append("no_vertebrae_segmented")
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

        # Hybrid: SpineNet for level identities + disc grading (TotalSegmentator
        # voxel segmentation above is untouched). Skipped when disabled / no
        # SAG_T2 DICOMs / any failure.
        spinenet_result = None
        spinenet_dicom_dir = preprocessed.get("spinenet_dicom_dir")
        if spinenet_dicom_dir:
            spinenet_result = self._run_spinenet(spinenet_dicom_dir)

        logger.info(
            "spine_mri_inference_complete",
            seg_shape=list(seg_array.shape),
            unique_labels=np.unique(seg_array).tolist(),
            inference_method=inference_method,
            spinenet_used=spinenet_result is not None,
        )

        return {
            "segmentation_path": seg_path,
            "segmentation_array": seg_array,
            "affine": affine,
            "image_shape": list(seg_array.shape),
            "inference_method": inference_method,
            "detected_vertebra_levels": detected_levels,
            "spinenet_result": spinenet_result,
            **{**preprocessed, "qa_flags": qa_flags},
        }

    def postprocess(
        self, inference_output: dict[str, Any], working_dir: str
    ) -> dict[str, Any]:
        logger.info("spine_mri_postprocess_start")

        seg_array = inference_output["segmentation_array"]
        affine = inference_output["affine"]
        label_map = self._config["postprocessing"]["label_map"]
        min_vol = self._config["postprocessing"].get("min_structure_volume_ml", 0.1)

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
        # disc_count: number of distinct disc components (label 2)
        disc_mask = (seg_clean == 2).astype(np.int32)
        _, disc_count = ndimage.label(disc_mask)

        # Vertebral levels — priority: SpineNet (specialist) > TotalSegmentator
        # per-vertebra identities > disc-count heuristic. SpineNet also supplies
        # per-disc radiological grading. TotalSegmentator voxel segmentation and
        # all volumes below are unaffected by this choice.
        spinenet_result = inference_output.get("spinenet_result") or {}
        spinenet_levels: list[str] = list(spinenet_result.get("levels", []) or [])
        disc_gradings: list[dict] = list(spinenet_result.get("disc_gradings", []) or [])
        ts_levels: list[str] = list(inference_output.get("detected_vertebra_levels", []) or [])

        # canal_area_mm2: mean axial cross-sectional area of spinal canal (label 3)
        canal_area_mm2 = self._mean_axial_area_mm2(seg_clean, label_id=3, voxel_spacing=voxel_spacing)

        # cord_area_mm2: mean axial cross-sectional area of spinal cord (label 4)
        cord_area_mm2 = self._mean_axial_area_mm2(seg_clean, label_id=4, voxel_spacing=voxel_spacing)

        if spinenet_levels:
            levels_analyzed = spinenet_levels
            levels_source = "spinenet"
            vertebra_count = int(spinenet_result.get("vertebra_count", len(spinenet_levels)))
        elif ts_levels:
            levels_analyzed = ts_levels
            levels_source = "totalsegmentator_identified"
            vertebra_count = len(ts_levels)
        else:
            levels_analyzed = self._infer_levels(disc_count)
            levels_source = "disc_count_heuristic"
            vert_mask = (seg_clean == 1).astype(np.int32)
            _, vertebra_count = ndimage.label(vert_mask)

        stenosis_suspected = (
            canal_area_mm2 > 0 and cord_area_mm2 > 0
            and (cord_area_mm2 / (canal_area_mm2 + 1e-6)) > 0.55
        )
        cord_compression_suspected = (
            cord_area_mm2 > 0 and canal_area_mm2 > 0
            and (cord_area_mm2 / (canal_area_mm2 + 1e-6)) > 0.70
        )

        # Save artifacts
        artifacts_dir = os.path.join(working_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        seg_nifti_path = os.path.join(artifacts_dir, "segmentation.nii.gz")
        seg_img = nib.Nifti1Image(seg_clean.astype(np.uint8), affine=affine)
        nib.save(seg_img, seg_nifti_path)

        report = {
            "summary": {
                "levels_analyzed": levels_analyzed,
                "levels_source": levels_source,
                "disc_gradings": disc_gradings,
                "disc_grading_available": bool(disc_gradings),
                "stenosis_suspected": stenosis_suspected,
                "cord_compression_suspected": cord_compression_suspected,
                "segmentation_labels": {str(k): v for k, v in label_map.items() if int(k) != 0},
                "sequences_used": inference_output.get("sequences_used", []),
                "inference_method": inference_output.get("inference_method", "unknown"),
                "processing_notes": self._generate_processing_notes(
                    inference_output.get("qa_flags", []),
                    disc_count,
                    stenosis_suspected,
                    cord_compression_suspected,
                    levels_analyzed,
                    levels_source=levels_source,
                    disc_gradings=disc_gradings,
                ),
            },
            "measurements": {
                "disc_count": disc_count,
                "canal_area_mm2": round(canal_area_mm2, 2),
                "cord_area_mm2": round(cord_area_mm2, 2),
                "vertebra_count": vertebra_count,
                "vertebra_levels_detected": levels_analyzed,
                "disc_gradings": disc_gradings,
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
            model_version_str = f"spine_mri_swinunetr_{Path(sw_path).stem}"
        elif architecture == "totalsegmentator_mr":
            task = self._config["model"].get("totalseg_task", "total_mr")
            model_version_str = f"totalsegmentator_{task}_v{model_version}"
        elif "no_model_weights" in qa_flags:
            model_version_str = f"spine_mri_synthetic_v{model_version}"
        else:
            model_version_str = f"spine_mri_v{model_version}"

        model_checksum = self._get_model_checksum()

        qa_details = dict(inference_output.get("qa_details", {}))
        qa_details["segmentation_stats"] = {
            "unique_labels": [int(x) for x in np.unique(seg_clean).tolist()],
            "disc_count": disc_count,
            "vertebra_count": vertebra_count,
            "canal_area_mm2": round(canal_area_mm2, 2),
            "cord_area_mm2": round(cord_area_mm2, 2),
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
            "spine_mri_postprocess_complete",
            disc_count=disc_count,
            vertebra_count=vertebra_count,
            stenosis_suspected=stenosis_suspected,
            cord_compression_suspected=cord_compression_suspected,
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
        """Intensity-thresholding synthetic segmentation for spine labels.

        Produces a plausible uint8 label map with four classes:
          1 = vertebra        (dense bone — high T1, moderate T2)
          2 = intervertebral_disc  (intermediate intensity, located between vertebrae)
          3 = spinal_canal    (low-intensity CSF-filled channel posterior to discs)
          4 = spinal_cord     (small oval of intermediate intensity inside canal)

        The approach:
        1. Percentile-threshold the input to isolate tissue vs background.
        2. Use morphological operations to approximate anatomical locations.
        3. Label connected components and assign the largest plausible region
           for each class.
        """
        arr = img_data.copy()
        seg = np.zeros(arr.shape, dtype=np.uint8)

        # Remove background — anything below the 10th non-zero percentile
        nonzero = arr[arr > 0]
        if nonzero.size == 0:
            return seg

        p10 = float(np.percentile(nonzero, 10))
        p40 = float(np.percentile(nonzero, 40))
        p65 = float(np.percentile(nonzero, 65))
        p80 = float(np.percentile(nonzero, 80))
        p90 = float(np.percentile(nonzero, 90))

        # ------------------------------------------------------------------
        # Label 1: Vertebra — bright structures (cortical/cancellous bone
        # in T1, dark in T2 but the model sees the full volume so we use the
        # top intensity tier as a proxy).
        # ------------------------------------------------------------------
        vertebra_raw = (arr >= p80).astype(np.int32)
        vertebra_labeled, n_vert = ndimage.label(vertebra_raw)
        if n_vert > 0:
            sizes = ndimage.sum(vertebra_raw, vertebra_labeled, range(1, n_vert + 1))
            # Keep the N largest components to simulate multiple vertebral bodies
            n_keep = min(n_vert, 15)
            top_ids = np.argsort(sizes)[::-1][:n_keep] + 1
            for vid in top_ids:
                seg[vertebra_labeled == vid] = 1

        # ------------------------------------------------------------------
        # Label 2: Intervertebral disc — intermediate intensity, sitting
        # between vertebrae (p40–p65 range, not already labelled as vertebra)
        # ------------------------------------------------------------------
        disc_raw = ((arr >= p40) & (arr < p65) & (seg == 0)).astype(np.int32)
        disc_labeled, n_disc = ndimage.label(disc_raw)
        if n_disc > 0:
            sizes = ndimage.sum(disc_raw, disc_labeled, range(1, n_disc + 1))
            n_keep = min(n_disc, 20)
            top_ids = np.argsort(sizes)[::-1][:n_keep] + 1
            for did in top_ids:
                seg[disc_labeled == did] = 2

        # ------------------------------------------------------------------
        # Label 3: Spinal canal — low-intensity (CSF): p10–p40, posterior
        # mid-line channel, not yet labelled.
        # ------------------------------------------------------------------
        canal_raw = ((arr >= p10) & (arr < p40) & (seg == 0)).astype(np.int32)
        canal_labeled, n_canal = ndimage.label(canal_raw)
        if n_canal > 0:
            sizes = ndimage.sum(canal_raw, canal_labeled, range(1, n_canal + 1))
            # Canal should be one continuous elongated channel — largest component
            largest_id = int(np.argmax(sizes)) + 1
            seg[canal_labeled == largest_id] = 3

        # ------------------------------------------------------------------
        # Label 4: Spinal cord — small oval structure inside the canal.
        # Simulate as the second-largest component in the canal range, eroded
        # to be smaller than the canal.
        # ------------------------------------------------------------------
        cord_candidate = ((arr >= p65) & (arr < p80) & (seg == 0)).astype(np.int32)
        cord_labeled, n_cord = ndimage.label(cord_candidate)
        if n_cord > 0:
            sizes = ndimage.sum(cord_candidate, cord_labeled, range(1, n_cord + 1))
            # The cord should be a single elongated structure — pick the
            # largest remaining candidate region.
            largest_id = int(np.argmax(sizes)) + 1
            cord_mask = (cord_labeled == largest_id).astype(np.int32)
            # Erode slightly to keep it inside the canal
            cord_eroded = ndimage.binary_erosion(
                cord_mask, structure=np.ones((3, 3, 3)), iterations=1
            )
            seg[cord_eroded] = 4

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
    def _mean_axial_area_mm2(
        seg: np.ndarray,
        label_id: int,
        voxel_spacing: np.ndarray,
    ) -> float:
        """Compute mean cross-sectional area in mm2 for a label across axial slices.

        Assumes the first axis is the superior–inferior (slice) direction.
        """
        mask = (seg == label_id)
        if not np.any(mask):
            return 0.0

        # In-plane voxel area (axes 1 and 2)
        in_plane_area_mm2 = float(voxel_spacing[1]) * float(voxel_spacing[2])

        # Count voxels per slice and average
        per_slice = mask.sum(axis=(1, 2))
        non_empty = per_slice[per_slice > 0]
        if non_empty.size == 0:
            return 0.0
        return round(float(np.mean(non_empty)) * in_plane_area_mm2, 2)

    @staticmethod
    def _parse_vertebra_level(filename: str) -> str | None:
        """Parse a TotalSegmentator vertebra filename into a level label.

        ``"vertebrae_L4.nii.gz"`` → ``"L4"``. Returns None if the parsed token is
        not a recognised vertebral level.
        """
        stem = filename.split(".")[0]
        level = stem.replace("vertebrae_", "").upper()
        return level if level in _ALL_LEVELS else None

    @staticmethod
    def _order_levels(levels: set[str]) -> list[str]:
        """Order a set of level labels superior→inferior (C1…S1)."""
        return [lv for lv in _ALL_LEVELS if lv in levels]

    @staticmethod
    def _infer_levels(disc_count: int) -> list[str]:
        """Map disc count to a plausible list of spinal levels.

        Uses a simplified heuristic: assumes the scan starts at the lowest
        cervical level and counts caudally.  Returns an empty list when
        disc_count is zero.
        """
        if disc_count == 0:
            return []
        levels: list[str] = []
        # Map from disc count to spine regions
        if disc_count <= 7:
            levels = _CERVICAL_LEVELS[:disc_count]
        elif disc_count <= 19:
            levels = _CERVICAL_LEVELS + _THORACIC_LEVELS[: disc_count - 7]
        else:
            n_lumbar = min(disc_count - 19, len(_LUMBAR_LEVELS + _SACRAL_LEVELS))
            levels = (
                _CERVICAL_LEVELS
                + _THORACIC_LEVELS
                + (_LUMBAR_LEVELS + _SACRAL_LEVELS)[:n_lumbar]
            )
        return levels

    @staticmethod
    def _generate_processing_notes(
        qa_flags: list[str],
        disc_count: int,
        stenosis_suspected: bool,
        cord_compression_suspected: bool,
        levels_analyzed: list[str] | None = None,
        levels_source: str = "disc_count_heuristic",
        disc_gradings: list[dict] | None = None,
    ) -> str:
        notes = []
        if levels_analyzed:
            source_label = {
                "spinenet": "SpineNet",
                "totalsegmentator_identified": "TotalSegmentator per-vertebra labels",
                "disc_count_heuristic": "disc-count estimate",
            }.get(levels_source, levels_source)
            notes.append(
                "Identified vertebral level(s): "
                + ", ".join(levels_analyzed)
                + f" ({source_label})."
            )
        # SpineNet disc grading summary (Pfirrmann + flagged pathologies).
        if disc_gradings:
            worst_pf = max((g.get("pfirrmann", 0) for g in disc_gradings), default=0)
            herniated = [g["level"] for g in disc_gradings if g.get("herniation", 0) > 0]
            notes.append(
                f"SpineNet graded {len(disc_gradings)} disc(s); worst Pfirrmann "
                f"grade {worst_pf}."
            )
            if herniated:
                notes.append("Disc herniation flagged at: " + ", ".join(herniated) + ".")
        if disc_count > 0:
            notes.append(f"Segmented {disc_count} intervertebral disc(s).")
        else:
            notes.append("No intervertebral discs detected in this study.")
        if stenosis_suspected:
            notes.append(
                "Cord-to-canal area ratio suggests possible spinal stenosis; "
                "clinical correlation recommended."
            )
        if cord_compression_suspected:
            notes.append(
                "Cord-to-canal area ratio exceeds compression threshold; "
                "urgent clinical review advised."
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
        if not qa_flags and disc_count > 0:
            notes.append("Processing completed normally with no quality concerns.")
        return " ".join(notes)
