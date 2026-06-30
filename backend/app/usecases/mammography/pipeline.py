from __future__ import annotations

"""Bilateral mammography pipeline.

  preprocess — classify the MG series into the four standard views (L/R x CC/MLO),
               download each, and render a windowed PNG for review.
  infer      — NYU GMIC breast classifier when weights are installed
               (config.model.custom_weights_path); otherwise a deterministic,
               NON-DIAGNOSTIC placeholder so the full worklist -> result -> report
               flow works end to end until weights are downloaded.
  postprocess— per-breast malignancy probability, AI-suggested BI-RADS, per-breast
               findings text (for the report template), and the rendered views as
               artifacts.

The model tier mirrors the other pipelines (e.g. pet_ct / chest_mri): a learned
model is attempted first and the pipeline degrades gracefully to the placeholder,
flagging the result non-diagnostic, rather than failing the job.
"""

import asyncio
import re
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import structlog
import yaml

from app.domain.interfaces import PACSClient
from app.domain.models import Series, Study
from app.usecases.base import BasePipeline

logger = structlog.get_logger(__name__)

USECASE_DIR = Path(__file__).parent
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

# Standard bilateral mammography views.
VIEWS = ["R_CC", "L_CC", "R_MLO", "L_MLO"]


def _classify_view(description: str) -> str | None:
    """Best-effort classification of an MG series description into a view code
    ('R_CC' | 'L_CC' | 'R_MLO' | 'L_MLO'), or None when undetermined."""
    d = (description or "").upper().replace("-", " ").replace("_", " ")
    if not d:
        return None

    if "MLO" in d or "MEDIOLAT" in d:
        view = "MLO"
    elif "CC" in d or "CRANIOCAUD" in d:
        view = "CC"
    else:
        return None

    # Laterality: prefer explicit words, then compact forms (RCC/LMLO), then a
    # standalone R/L token.
    has_right = bool(re.search(r"\bRIGHT\b|\bRT\b|\bR(?:CC|MLO)\b", d))
    has_left = bool(re.search(r"\bLEFT\b|\bLT\b|\bL(?:CC|MLO)\b", d))
    if not has_right and not has_left:
        tok_r = re.search(r"(?<![A-Z])R(?![A-Z])", d)
        tok_l = re.search(r"(?<![A-Z])L(?![A-Z])", d)
        has_right, has_left = bool(tok_r), bool(tok_l)

    if has_right and not has_left:
        return f"R_{view}"
    if has_left and not has_right:
        return f"L_{view}"
    return None


class Pipeline(BasePipeline):
    """Mammography analysis pipeline (GMIC when available, else placeholder)."""

    def __init__(self) -> None:
        with open(CONFIG_PATH) as fh:
            self._config: dict[str, Any] = yaml.safe_load(fh)

        self._cfg_model = self._config.get("model", {})
        self._cfg_inf = self._config.get("inference", {})
        self._cfg_pre = self._config.get("preprocessing", {})
        self._cfg_qa = self._config.get("quality_checks", {})

        self._model = None  # GMIC model when loaded; None -> placeholder
        self._model_checksum_cache: str | None = None

        weights = self._cfg_model.get("custom_weights_path")
        if self._cfg_model.get("architecture") == "gmic" and weights and Path(weights).exists():
            try:
                self._load_gmic(weights)
            except Exception as exc:  # never block; fall back to placeholder
                logger.warning("gmic_load_failed_using_placeholder", error=str(exc))
                self._model = None

    # ── Model loading (real model goes here once weights are downloaded) ────────

    # GMIC input size and per-model top-t% (from the NYU GMIC release).
    _GMIC_INPUT = (2944, 1920)
    _GMIC_PERCENT_T = {"1": 0.02, "2": 0.03, "3": 0.03, "4": 0.05, "5": 0.1}

    def _load_gmic(self, weights_path: str) -> None:
        """Load the NYU GMIC breast classifier (5-model ensemble).

        The vendored architecture lives at backend/external/GMIC and uses
        absolute ``src.*`` imports, so that directory is put on sys.path before
        importing. Each ``sample_model_{i}.p`` is the raw state_dict (loaded with
        strict=False, as in GMIC's run_model.py)."""
        import sys

        import torch

        ext_dir = Path(__file__).resolve().parents[3] / "external"
        gmic_root = next(
            (p for p in ext_dir.iterdir() if p.is_dir() and p.name.lower() == "gmic"),
            None,
        )
        if gmic_root is None:
            raise FileNotFoundError(f"GMIC vendored code not found under {ext_dir}")
        if str(gmic_root) not in sys.path:
            sys.path.insert(0, str(gmic_root))

        # GMIC env shims (so its cropping/loading modules import under our deps):
        #  - scipy>=1.14 removed scipy.ndimage.morphology (binary_erosion/dilation
        #    are now top-level in scipy.ndimage);
        #  - reading_images imports h5py only for the hdf5 path, which we never use.
        import types

        import scipy.ndimage as _ndi
        if not hasattr(_ndi, "morphology"):
            _ndi.morphology = _ndi
        sys.modules.setdefault("h5py", types.ModuleType("h5py"))

        from src.modeling.gmic import GMIC  # vendored architecture

        use_gpu = torch.cuda.is_available()
        self._device = torch.device("cuda:0" if use_gpu else "cpu")
        base = {
            "device_type": "gpu" if use_gpu else "cpu",
            "gpu_number": 0,
            "max_crop_noise": (100, 100),
            "max_crop_size_noise": 100,
            "cam_size": (46, 30),
            "K": 6,
            "crop_shape": (256, 256),
            "post_processing_dim": 256,
            "num_classes": 2,
            "use_v1_global": False,
        }
        wdir = Path(weights_path)
        models = []
        for i in range(1, 6):
            params = dict(base)
            params["percent_t"] = self._GMIC_PERCENT_T[str(i)]
            model = GMIC(params)
            state = torch.load(str(wdir / f"sample_model_{i}.p"), map_location=self._device)
            model.load_state_dict(state, strict=False)
            model.eval().to(self._device)
            models.append(model)
        self._model = models
        self._model_checksum_cache = "gmic_ensemble_5"
        logger.info("gmic_loaded", n_models=len(models), device=str(self._device))

    def _run_gmic(self, view_nifti: dict[str, str]) -> dict[str, float | None]:
        """Per-breast malignancy probability = mean over the 5-model ensemble and
        over that breast's views. GMIC's y_fusion is already sigmoid'd; column 1 is
        the malignant probability (column 0 is benign), per run_model.py."""
        import torch

        self._n_fallback_views = 0
        per_side: dict[str, list[float]] = {"R": [], "L": []}
        for view_code, nifti_path in view_nifti.items():
            side = view_code.split("_")[0]
            if side not in per_side:
                continue
            x = self._gmic_preprocess(nifti_path, view_code).to(self._device)
            mal: list[float] = []
            with torch.no_grad():
                for model in self._model:
                    out = model(x)  # (1, 2): [benign, malignant], already sigmoid'd
                    mal.append(float(out[0, 1].item()))
            if mal:
                per_side[side].append(sum(mal) / len(mal))
        return {
            s: (round(sum(v) / len(v), 3) if v else None) for s, v in per_side.items()
        }

    def _gmic_preprocess(self, nifti_path: str, view_code: str):
        """View NIfTI -> (1,1,2944,1920) float tensor using GMIC's FAITHFUL
        preprocessing: breast crop (largest connected component) -> optimal-center
        window -> view flip -> standardize, via GMIC's own vendored functions.

        Falls back to a plain resize for a view if the crop/center step fails
        (e.g. degenerate image), counted in ``self._n_fallback_views``."""
        import torch

        arr = np.squeeze(np.asarray(nib.load(nifti_path).get_fdata(), dtype=np.float32))
        if arr.ndim == 3:
            axis = int(np.argmin(arr.shape))
            arr = np.take(arr, arr.shape[axis] // 2, axis=axis)
        arr = np.clip(arr, 0.0, None)  # GMIC's crop mask keys on img > 0

        try:
            from src.cropping.crop_mammogram import (
                crop_img_from_largest_connected,
                image_orientation,
            )
            from src.optimal_centers.get_optimal_centers import extract_center
            import src.data_loading.loading as loading

            full_view = view_code.replace("_", "-")  # R_CC -> R-CC
            mode = image_orientation("NO", full_view[0])
            crop_info = crop_img_from_largest_connected(arr, mode, True, 100, 50, 1.0 / 3)
            top, bottom, left, right = crop_info[0]
            cropped = arr[top:bottom, left:right]
            meta = {
                "full_view": full_view,
                "horizontal_flip": "NO",
                "view": full_view[2:],
                "rightmost_points": crop_info[1],
                "bottommost_points": crop_info[2],
            }
            best_center = extract_center(meta, cropped)
            proc = loading.process_image(
                loading.flip_image(cropped, full_view, "NO"), full_view, best_center
            )
            return torch.from_numpy(np.ascontiguousarray(proc))[None, None].float()
        except Exception as exc:
            logger.warning(
                "gmic_faithful_preprocess_failed_using_resize",
                view=view_code, error=str(exc),
            )
            self._n_fallback_views = getattr(self, "_n_fallback_views", 0) + 1
            return self._gmic_resize_fallback(arr)

    def _gmic_resize_fallback(self, arr: np.ndarray):
        """Plain standardize + resize to GMIC input size (used only when the
        faithful crop fails for a view)."""
        import torch
        import torch.nn.functional as F

        nz = arr[arr > 0]
        if nz.size:
            arr = (arr - float(nz.mean())) / (float(nz.std()) or 1.0)
        t = torch.from_numpy(np.ascontiguousarray(arr))[None, None].float()
        return F.interpolate(t, size=self._GMIC_INPUT, mode="bilinear", align_corners=False)

    # ── Phase 1: preprocess ─────────────────────────────────────────────────────

    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        logger.info("mammography_preprocess_start", study_uid=study.study_instance_uid)
        loop = event_loop or asyncio.get_event_loop()

        nifti_dir = Path(working_dir) / "nifti"
        artifacts_dir = Path(working_dir) / "artifacts"
        nifti_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Keep only MG series; classify each into a view.
        mg_series = [s for s in series if (s.modality or "").upper() == "MG"] or series

        qa_flags: list[str] = []
        qa_details: dict[str, Any] = {}

        view_pngs: dict[str, str] = {}    # view_code -> png path
        view_arrays: dict[str, str] = {}  # view_code -> nifti path
        unclassified = 0

        for idx, s in enumerate(mg_series):
            view = _classify_view(s.series_description or s.protocol_name or "")
            key = view or f"VIEW_{idx + 1}"
            if view is None:
                unclassified += 1
            nifti_path = nifti_dir / f"{key}.nii.gz"
            try:
                loop.run_until_complete(
                    pacs.download_series_as_nifti(
                        study.study_instance_uid, s.series_instance_uid, str(nifti_path)
                    )
                )
            except Exception as exc:
                logger.warning("mg_series_download_failed", view=key, error=str(exc))
                qa_flags.append("series_download_failed")
                continue

            png_path = artifacts_dir / f"{key}.png"
            if self._render_png(str(nifti_path), str(png_path)) is not None:
                view_pngs[key] = str(png_path)
                view_arrays[key] = str(nifti_path)

        if unclassified:
            qa_flags.append("view_classification_uncertain")
            qa_details["unclassified_views"] = unclassified

        # Laterality-aware QA. A study may be intentionally unilateral (right- or
        # left-only) — that's not "missing views". Flag only a PRESENT breast that
        # lacks one of its two standard views (CC/MLO).
        classified_views = [v for v in view_arrays if v in VIEWS]
        sides_present = {v.split("_")[0] for v in classified_views}
        if len(sides_present) == 1:
            qa_flags.append("unilateral_study")
            qa_details["laterality"] = "right" if "R" in sides_present else "left"
        for side in sides_present:
            side_views = {v for v in classified_views if v.startswith(f"{side}_")}
            if len(side_views) < 2:
                qa_flags.append("incomplete_views")
                qa_details.setdefault("incomplete_sides", []).append(side)
        qa_details["views_found"] = sorted(view_arrays.keys())

        logger.info(
            "mammography_preprocess_complete",
            views=sorted(view_arrays.keys()),
            qa_flags=qa_flags,
        )
        return {
            "view_nifti_paths": view_arrays,
            "view_png_paths": view_pngs,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "study_uid": study.study_instance_uid,
        }

    def _render_png(self, nifti_path: str, out_path: str) -> np.ndarray | None:
        """Render a (2D) mammogram NIfTI to a windowed grayscale PNG. Returns the
        2D array (for placeholder stats) or None on failure."""
        try:
            from PIL import Image

            arr = np.squeeze(np.asarray(nib.load(nifti_path).get_fdata(), dtype=np.float32))
            if arr.ndim == 3:
                axis = int(np.argmin(arr.shape))
                arr = np.take(arr, arr.shape[axis] // 2, axis=axis)
            if arr.ndim != 2:
                return None

            lo = float(np.percentile(arr, self._cfg_pre.get("window_low_percentile", 1.0)))
            hi = float(np.percentile(arr, self._cfg_pre.get("window_high_percentile", 99.0)))
            norm = np.clip((arr - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
            img = Image.fromarray((norm * 255.0).astype(np.uint8), mode="L")

            out_size = int(self._cfg_pre.get("out_size", 1024))
            long_edge = max(img.size)
            if out_size and long_edge > 0 and long_edge != out_size:
                scale = out_size / long_edge
                img = img.resize(
                    (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
                    Image.BILINEAR,
                )
            img.save(out_path, format="PNG")
            return arr
        except Exception as exc:
            logger.warning("mammo_render_failed", path=nifti_path, error=str(exc))
            return None

    # ── Phase 2: infer ───────────────────────────────────────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("mammography_inference_start")
        view_nifti = preprocessed.get("view_nifti_paths", {})

        if self._model is not None:
            # Real GMIC path.
            try:
                scores = self._run_gmic(view_nifti)
                qa = list(preprocessed.get("qa_flags", []))
                if getattr(self, "_n_fallback_views", 0) > 0:
                    qa.append("gmic_partial_resize_fallback")
                return {
                    **preprocessed,
                    "qa_flags": qa,
                    "scores": scores,
                    "inference_method": "gmic",
                }
            except Exception as exc:
                logger.warning("gmic_inference_failed_using_placeholder", error=str(exc))

        # Deterministic, NON-DIAGNOSTIC placeholder. Per-breast pseudo-probability
        # derived from view intensity statistics — stable for a given study, kept
        # in a low/benign range so it is never read as an alarming finding.
        per_side: dict[str, list[float]] = {"R": [], "L": []}
        for view_code, nifti_path in view_nifti.items():
            side = view_code.split("_")[0]
            if side not in per_side:
                continue
            arr = np.squeeze(np.asarray(nib.load(nifti_path).get_fdata(), dtype=np.float32))
            per_side[side].append(self._placeholder_prob(arr))

        def _agg(vals: list[float]) -> float | None:
            return round(float(np.mean(vals)), 3) if vals else None

        scores = {"R": _agg(per_side["R"]), "L": _agg(per_side["L"])}
        logger.info("mammography_inference_complete", method="placeholder", scores=scores)
        return {**preprocessed, "scores": scores, "inference_method": "placeholder"}

    @staticmethod
    def _placeholder_prob(arr: np.ndarray) -> float:
        if arr is None or arr.size == 0:
            return 0.1
        vals = arr[arr > 0]
        if vals.size == 0:
            return 0.1
        frac = float(np.mean(vals)) / (float(np.max(vals)) or 1.0)
        return round(min(0.45, max(0.05, 0.05 + frac * 0.30)), 3)

    # ── Phase 3: postprocess ──────────────────────────────────────────────────────

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("mammography_postprocess_start")
        method = inference_output.get("inference_method", "placeholder")
        is_placeholder = method == "placeholder"
        scores: dict[str, float | None] = inference_output.get("scores", {}) or {}
        view_pngs: dict[str, str] = inference_output.get("view_png_paths", {})

        prob_r = scores.get("R")
        prob_l = scores.get("L")
        # Laterality is determined by which breast(s) were actually imaged: a side
        # with no views yields a None score.
        present_r = prob_r is not None
        present_l = prob_l is not None
        laterality = (
            "bilateral" if (present_r and present_l)
            else "right" if present_r
            else "left" if present_l
            else "unknown"
        )

        birads_r = self._suggest_birads(prob_r) if present_r else None
        birads_l = self._suggest_birads(prob_l) if present_l else None

        # Findings only for imaged breast(s); the absent side is left null so the
        # report renders as a true unilateral study rather than "bilateral, blank".
        findings_r = self._findings_text("right", prob_r, birads_r, is_placeholder) if present_r else None
        findings_l = self._findings_text("left", prob_l, birads_l, is_placeholder) if present_l else None
        opinion = self._opinion_text(birads_r, birads_l, is_placeholder, laterality)

        qa_flags = list(inference_output.get("qa_flags", []))
        qa_details = dict(inference_output.get("qa_details", {}))
        if is_placeholder:
            qa_flags.append("placeholder_no_model")

        notes = (
            "NON-DIAGNOSTIC placeholder — no mammography model weights installed. "
            "Findings and BI-RADS are stubs for the radiologist to complete."
            if is_placeholder
            else "AI-assisted mammography analysis."
        )

        artifacts = [
            {
                "name": Path(p).name,
                "artifact_type": "mammo_png",
                "local_path": p,
                "content_type": "image/png",
            }
            for p in sorted(view_pngs.values())
        ]

        version = self._cfg_model.get("version", "1.0.0")
        model_version = (
            f"mammography_placeholder_v{version}" if is_placeholder else f"mammography_gmic_v{version}"
        )

        return {
            "summary": {
                "laterality": laterality,
                "birads_right": birads_r,
                "birads_left": birads_l,
                "malignancy_probability_right": prob_r,
                "malignancy_probability_left": prob_l,
                "density_right": None,
                "density_left": None,
                "right_breast_findings": findings_r,
                "left_breast_findings": findings_l,
                "opinion": opinion,
                "quantitative": not is_placeholder,
                "inference_method": method,
                "processing_notes": notes,
            },
            "measurements": {
                "malignancy_probability_right": prob_r,
                "malignancy_probability_left": prob_l,
                "views_analyzed": sorted(inference_output.get("view_nifti_paths", {}).keys()),
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": model_version,
            "model_checksum": self._model_checksum_cache or "placeholder",
            "artifacts": artifacts,
        }

    def _suggest_birads(self, prob: float | None) -> int | None:
        if prob is None:
            return None
        for tier in self._cfg_inf.get("birads_thresholds", []) or self._config.get("birads_thresholds", []):
            if prob < float(tier["max_prob"]):
                return int(tier["birads"])
        return 5

    @staticmethod
    def _findings_text(side: str, prob: float | None, birads: int | None, placeholder: bool) -> str:
        if placeholder:
            return (
                f"AI placeholder (no model loaded): no automated assessment of the {side} breast "
                "was performed. Mammographic parenchyma, masses, calcifications and asymmetries "
                "require radiologist review."
            )
        if prob is None:
            return f"The {side} breast was not assessable from the available views."
        return (
            f"Automated {side}-breast malignancy probability {prob:.2f} "
            f"(AI-suggested BI-RADS {birads}). Radiologist correlation required."
        )

    @staticmethod
    def _opinion_text(
        birads_r: int | None, birads_l: int | None, placeholder: bool, laterality: str = "bilateral"
    ) -> str:
        scope = {"right": "right breast", "left": "left breast"}.get(laterality, "both breasts")
        if placeholder:
            return (
                f"NON-DIAGNOSTIC placeholder result ({scope}) — mammography model not yet "
                "installed. To be completed by the reporting radiologist. Advise ultrasound "
                "correlation as indicated."
            )
        parts = []
        if birads_r is not None:
            parts.append(f"Right breast AI-suggested BI-RADS {birads_r}")
        if birads_l is not None:
            parts.append(f"Left breast AI-suggested BI-RADS {birads_l}")
        return ". ".join(parts) + ". Radiologist review required to assign final BI-RADS."
