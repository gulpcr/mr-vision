from __future__ import annotations

"""Abdomen CT → MedGemma pipeline.

No learned model, no segmentation, no measurement. It takes the study's primary
CT series straight from PACS, samples a set of axial slices EVENLY across the
scanned volume (superior→inferior, so coverage is consistent regardless of slice
thickness), renders each in a FIXED soft-tissue HU window (level 40 / width 400 —
the portal-venous workhorse window), and exposes them as artifacts. The free-text
radiological report is authored afterwards by a local MedGemma vision-language
model in the Celery task hook (infrastructure/queue/tasks.py), which reads these
PNGs from disk — mirroring how the pet_ct MedGemma path works.

Design choices (see the conversation that produced this):
  * Fixed HU windowing, not percentile auto-windowing — percentile flattens the
    low-contrast difference between a lesion and normal parenchyma.
  * Even-coverage sampling by fraction of depth, not fixed z indices — a study is
    60–500 slices depending on thickness, so fixed indices land on random anatomy.
  * MedGemma names the organs itself from the windowed slices (no TotalSegmentator);
    the trade-off is that a small organ can fall between sampled slices, mitigated
    by sampling more slices. This is assistive, NON-DIAGNOSTIC output.

Layer note: like the other plugins this module stays free of infrastructure
imports (the MedGemma call lives in the Celery task). Its own prompt builder is in
``app.usecases.abdomen_ct.prompts`` and is self-contained — no cross-plugin imports.
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

# CT series selection: prefer volumetric CT, skip single-plane scouts/topograms.
_CT_SCOUT_PATTERNS = [
    r"(?i)topogram", r"(?i)scout", r"(?i)localizer", r"(?i)localiser",
    r"(?i)surview", r"(?i)scanogram", r"(?i)\bscano\b",
]


class Pipeline(BasePipeline):
    """Samples soft-tissue-windowed abdomen CT slices; MedGemma narrates them later."""

    def __init__(self) -> None:
        with open(CONFIG_PATH) as fh:
            self._cfg: dict[str, Any] = yaml.safe_load(fh) or {}
        self._cfg_pre = self._cfg.get("preprocessing", {})
        self._cfg_qa = self._cfg.get("quality_checks", {})
        self._model_version = "abdomen_ct_medgemma_v1.0.0"
        self._model_checksum = "n/a_no_model"

        # Optional anomaly-guided slice selector (VQVAE + Transformer). Gated by the
        # Settings flag AND the plugin's own anomaly.enabled AND weights on disk;
        # any load failure leaves it None so infer() falls back to even sampling.
        self._selector = None
        self._load_anomaly_selector()

    def _load_anomaly_selector(self) -> None:
        from app.config import get_settings

        cfg = self._cfg.get("anomaly", {}) or {}
        if not (get_settings().abdomen_ct_anomaly_enabled and cfg.get("enabled", False)):
            return

        vq = USECASE_DIR / cfg.get("vqvae_weights", "model/vqvae_last.pt")
        tr = USECASE_DIR / cfg.get("transformer_weights", "model/transformer_last.pt")
        if not (vq.exists() and tr.exists()):
            logger.warning("abdomen_ct_anomaly_weights_missing", vqvae=str(vq), transformer=str(tr))
            return

        try:
            from app.usecases.abdomen_ct.anomaly import AnomalySliceSelector

            selector = AnomalySliceSelector(cfg, USECASE_DIR)
            selector.load()
            self._selector = selector
        except Exception as exc:
            logger.warning("abdomen_ct_anomaly_load_failed_using_even_sampling", error=str(exc))
            self._selector = None

    # ── series selection ─────────────────────────────────────────────────────

    @staticmethod
    def _select_primary_series(series: list[Series]) -> Series | None:
        """Pick the diagnostic CT volume: most instances, excluding scouts.

        Prefers CT-modality series; skips single/dual-slice scouts/topograms. Falls
        back to the largest series of any modality if no CT is tagged.
        """
        if not series:
            return None

        def _instances(s: Series) -> int:
            return getattr(s, "num_instances", 0) or 0

        def _is_scout(s: Series) -> bool:
            desc = (s.series_description or "").strip()
            n = _instances(s)
            return any(re.search(p, desc) for p in _CT_SCOUT_PATTERNS) or (0 < n <= 2)

        ct = [s for s in series if (s.modality or "").upper() == "CT" and not _is_scout(s)]
        pool = ct or [s for s in series if not _is_scout(s)] or series
        return max(pool, key=_instances)

    # ── Phase 1: preprocess (download the primary CT series) ───────────────────

    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        loop = event_loop or asyncio.get_event_loop()

        primary = self._select_primary_series(series)
        if primary is None:
            raise ValueError("No series found for abdomen_ct pipeline")

        nifti_dir = Path(working_dir) / "nifti"
        nifti_dir.mkdir(parents=True, exist_ok=True)
        volume_path = str(nifti_dir / "volume.nii.gz")

        loop.run_until_complete(
            pacs.download_series_as_nifti(
                study.study_instance_uid, primary.series_instance_uid, volume_path
            )
        )

        logger.info(
            "abdomen_ct_preprocess_complete",
            study_uid=study.study_instance_uid,
            series=primary.series_description,
            modality=primary.modality,
            instances=getattr(primary, "num_instances", None),
        )

        return {
            "volume_path": volume_path,
            "study_uid": study.study_instance_uid,
            "modality": (primary.modality or study.modality or "").upper() or None,
            "series_description": primary.series_description,
            "study_description": study.study_description,
            "qa_flags": [],
            "qa_details": {},
        }

    # ── Phase 2: infer (anomaly-guided slice selection, or pass-through) ─────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        out = dict(preprocessed)
        if self._selector is None or not self._selector.ready:
            logger.info("abdomen_ct_infer_even_sampling")
            return out

        try:
            arr = self._load_volume(preprocessed["volume_path"])
            if arr.ndim != 3:
                logger.info("abdomen_ct_anomaly_skipped_not_a_volume")
                return out
            z_lo, z_hi = self._trim_bounds(arr.shape[2])
            top_k = max(1, int(self._cfg_pre.get("slice_count", 6)))
            result = self._selector.select(arr, top_k, z_lo, z_hi)
            if result and result.get("selected_z"):
                out["anomaly_selected_z"] = result["selected_z"]
                out["anomaly_scores"] = result["scores"]
                out["anomaly_scored_count"] = result["scored_count"]
                out["model_version"] = result["model_version"]
                out["model_checksum"] = result["model_checksum"]
        except Exception as exc:
            logger.warning("abdomen_ct_anomaly_infer_failed_fallback", error=str(exc))
        return out

    # ── Phase 3: postprocess (sample + window the slices) ───────────────────────

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("abdomen_ct_postprocess_start")

        artifacts_dir = Path(working_dir) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        arr = self._load_volume(inference_output["volume_path"])
        windows = self._windows()

        # Slice indices come from the anomaly selector when it ran in infer(); otherwise
        # fall back to even-across-volume sampling. Either way we render the RAW slices.
        anomaly_z = inference_output.get("anomaly_selected_z")
        if anomaly_z:
            z_indices = [int(z) for z in anomaly_z]
            selection_method = "anomaly"
        else:
            z_indices = self._sample_slice_indices(arr)
            selection_method = "even"

        if arr.ndim < 3:
            qa_flags.append("not_a_volume")
        elif arr.shape[2] < int(self._cfg_qa.get("min_slices", 10)):
            qa_flags.append("insufficient_slices")

        # Render each sampled LEVEL in each configured HU window. Ordering is
        # level-major (all windows of level 1, then level 2, …) so same-level windows
        # stay adjacent in the image list the model receives.
        artifacts: list[dict[str, Any]] = []
        levels: list[int] = []
        for order, z in enumerate(z_indices, 1):
            slice_2d = self._extract_slice(arr, z)
            if slice_2d is None:
                continue
            level_rendered = False
            for w in windows:
                name = f"slice_{order:02d}_z{z:04d}_{self._slug(w['name'])}.png"
                out_path = artifacts_dir / name
                if self._render_png(slice_2d, str(out_path), float(w["level"]), float(w["width"])):
                    level_rendered = True
                    artifacts.append({
                        "name": name,
                        "artifact_type": "abdomen_ct_slice_png",
                        "local_path": str(out_path),
                        "content_type": "image/png",
                    })
            if level_rendered:
                levels.append(z)

        if not artifacts:
            qa_flags.append("no_slices_rendered")

        image_dimensions = [int(d) for d in arr.shape]
        window_names = [str(w["name"]) for w in windows]
        window_desc = ", ".join(
            f"{w['name']} (L{float(w['level']):g}/W{float(w['width']):g})" for w in windows
        )
        anomaly_scores = inference_output.get("anomaly_scores") or {}
        if selection_method == "anomaly":
            top = ", ".join(f"z{z} (nll {anomaly_scores.get(z, anomaly_scores.get(int(z))):.2f})"
                            for z in levels if anomaly_scores.get(z, anomaly_scores.get(int(z))) is not None)
            inference_method = "vqvae_transformer_anomaly_slice_selection"
            selection_note = (
                f"Selected {len(levels)} MOST-ANOMALOUS axial level(s) via an unsupervised "
                f"VQVAE+Transformer (of {inference_output.get('anomaly_scored_count', '?')} scored): "
                f"{top or 'z=' + str(levels)}"
            )
        else:
            inference_method = "even_sampling (multi-window slices → MedGemma)"
            selection_note = f"Sampled {len(levels)} axial level(s) EVENLY across the volume (z={levels})"

        summary = {
            "modality": inference_output.get("modality"),
            "series_description": inference_output.get("series_description"),
            "study_description": inference_output.get("study_description"),
            "slices_rendered": levels,
            "slice_selection": selection_method,
            "anomaly_scores": {int(z): anomaly_scores[z] for z in anomaly_scores} or None,
            "image_dimensions": image_dimensions,
            "hu_windows": window_names,
            "quantitative": False,
            "inference_method": inference_method,
            "processing_notes": (
                f"{selection_note}, each rendered in {len(windows)} HU window(s): {window_desc}. "
                f"{len(artifacts)} image(s) total. A free-text radiological report is authored "
                "by the local MedGemma model when enabled. NON-DIAGNOSTIC: only the selected "
                "slices were reviewed — pathology on unshown slices cannot be excluded."
            ),
        }

        logger.info(
            "abdomen_ct_postprocess_complete",
            levels_sampled=levels, images_total=len(artifacts),
            image_dimensions=image_dimensions, qa_flags=qa_flags,
        )

        return {
            "summary": summary,
            "measurements": {
                "image_dimensions": image_dimensions,
                "slices_rendered": levels,
                "images_total": len(artifacts),
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": inference_output.get("model_version", self._model_version),
            "model_checksum": inference_output.get("model_checksum", self._model_checksum),
            "artifacts": artifacts,
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    _DEFAULT_WINDOWS = [{"name": "soft-tissue", "level": 40.0, "width": 400.0}]

    def _windows(self) -> list[dict[str, Any]]:
        """Configured HU windows, or a soft-tissue default. Skips malformed entries."""
        raw = self._cfg_pre.get("windows")
        if not isinstance(raw, list) or not raw:
            return list(self._DEFAULT_WINDOWS)
        out: list[dict[str, Any]] = []
        for w in raw:
            if isinstance(w, dict) and "level" in w and "width" in w:
                out.append({"name": str(w.get("name", "window")), "level": w["level"], "width": w["width"]})
        return out or list(self._DEFAULT_WINDOWS)

    @staticmethod
    def _slug(name: str) -> str:
        """Filesystem/URL-safe slug for a window name (e.g. 'soft-tissue' → 'soft-tissue')."""
        return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "window"

    @staticmethod
    def _load_volume(volume_path: str) -> np.ndarray:
        """Load the NIfTI as a scalar float32 volume, collapsing any channel/time axis."""
        arr = np.squeeze(np.asarray(nib.load(volume_path).get_fdata(), dtype=np.float32))
        while arr.ndim > 3:
            arr = arr[..., 0]
        return arr

    def _trim_bounds(self, nz: int) -> tuple[int, int]:
        """Inclusive [lo, hi] z range after trimming partial-anatomy/table ends."""
        trim = float(self._cfg_pre.get("edge_trim_fraction", 0.08))
        lo = int(nz * trim)
        hi = int(nz * (1.0 - trim)) - 1
        if hi <= lo:
            lo, hi = 0, nz - 1
        return lo, hi

    def _sample_slice_indices(self, arr: np.ndarray) -> list[int]:
        """Evenly-spaced z indices across the volume (superior→inferior).

        Trims a fraction off each end (partial anatomy / table) and returns
        ``slice_count`` unique indices. For a 2D image returns the single slice.
        """
        if arr.ndim < 3:
            return [0]

        nz = int(arr.shape[2])
        count = max(1, int(self._cfg_pre.get("slice_count", 6)))
        lo, hi = self._trim_bounds(nz)
        if count == 1:
            return [(lo + hi) // 2]
        # NIfTI z is stored inferior→superior; sample from hi (superior) down to lo
        # so image order 1..N reads superior→inferior, matching radiological reading.
        idx = np.linspace(hi, lo, count).round().astype(int)
        seen: list[int] = []
        for z in idx:
            z = int(max(0, min(nz - 1, z)))
            if z not in seen:
                seen.append(z)
        return seen

    @staticmethod
    def _extract_slice(arr: np.ndarray, z: int) -> np.ndarray | None:
        """Axial slice at index z along the last axis (z-axis)."""
        if arr.ndim == 2:
            return arr
        if arr.ndim == 3 and 0 <= z < arr.shape[2]:
            return arr[:, :, z]
        return None

    def _render_png(self, slice_2d: np.ndarray, out_path: str, level: float, width: float) -> bool:
        """Window a 2D CT slice to a fixed HU window and write a grayscale PNG.

        HU window is (level - width/2, level + width/2); values outside are clamped.
        True on success.
        """
        try:
            from PIL import Image

            slice_2d = np.asarray(slice_2d, dtype=np.float32)
            if slice_2d.ndim != 2 or slice_2d.size == 0:
                return False

            lo = level - width / 2.0
            hi = level + width / 2.0
            norm = np.clip((slice_2d - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
            # Radiological display convention: rows top-to-bottom (patient anterior
            # up), so transpose then flip the vertical axis nibabel stores bottom-up.
            img = Image.fromarray((np.flipud(norm.T) * 255.0).astype(np.uint8), mode="L")

            out_size = int(self._cfg_pre.get("out_size", 1024) or 0)
            long_edge = max(img.size)
            if out_size and long_edge and long_edge != out_size:
                scale = out_size / long_edge
                img = img.resize(
                    (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
                    Image.BILINEAR,
                )
            img.save(out_path, format="PNG")
            return True
        except Exception as exc:
            logger.warning("abdomen_ct_render_failed", error=str(exc))
            return False
