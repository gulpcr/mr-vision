from __future__ import annotations

"""Abdomen CT (v4) → two-stage, two-model local multi-agent pipeline.

This is the platform-facing ``BasePipeline`` for abdomen_ct4. Like abdomen_ct2 it owns
Pass-0 only — CT series selection + rendering the ordered scannable volume — and leaves
the two-stage MedGemma work to the Celery task hook, which drives the
:class:`~app.usecases.abdomen_ct4.multi_agent_pipeline.CTAbdomen4Pipeline` engine.

The split mirrors abdomen_ct2 (pipeline renders / hook infers) so the heavy model I/O
stays in the worker and this module keeps to the strict use-case layer (no infrastructure
imports). Where abdomen_ct2 batches single slices to one Ollama model, abdomen_ct4 renders
one ordered soft-tissue stack and the hook feeds it to a two-model engine: a 4B multimodal
triage stage over overlapping 40-slice blocks, then a 27B text-only reporting stage.

The rendered slices go to ``working_dir/scan/`` and are described by an ordered
``scan_slices`` manifest returned to the hook (position ``order`` = the engine's absolute
index; ``z`` = the source axial level). Only a small preview subset is registered as
artifacts here; the hook appends the flagged slices before the MinIO upload.

NON-DIAGNOSTIC assistive output — full-volume radiologist review remains required.
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
    """Renders the ordered abdomen CT volume; the two-model engine flags + reports later."""

    def __init__(self) -> None:
        cfg: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as fh:
                cfg = yaml.safe_load(fh) or {}
        self._cfg = cfg
        self._cfg_pre = cfg.get("preprocessing", {})
        self._cfg_scan = cfg.get("scan", {})
        self._cfg_qa = cfg.get("quality_checks", {})
        self._model_version = "abdomen_ct4_multiagent_v1.0.0"
        self._model_checksum = "n/a_no_local_weights"

    # ── series selection (same policy as abdomen_ct2) ──────────────────────────

    @staticmethod
    def _select_primary_series(series: list[Series]) -> Series | None:
        """Pick the diagnostic CT volume: most instances, excluding scouts/topograms."""
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
            raise ValueError("No series found for abdomen_ct4 pipeline")

        nifti_dir = Path(working_dir) / "nifti"
        nifti_dir.mkdir(parents=True, exist_ok=True)
        volume_path = str(nifti_dir / "volume.nii.gz")

        loop.run_until_complete(
            pacs.download_series_as_nifti(
                study.study_instance_uid, primary.series_instance_uid, volume_path
            )
        )

        logger.info(
            "abdomen_ct4_preprocess_complete",
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

    # ── Phase 2: infer (no learned model here — the engine runs in the hook) ────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        # The "inference" for abdomen_ct4 is the two-stage engine (4B triage + 27B
        # report), which runs in the Celery task hook because it owns the heavy local
        # model I/O. Here we only pass the preprocessed context through.
        logger.info("abdomen_ct4_infer_passthrough")
        return dict(preprocessed)

    # ── Phase 3: postprocess (render the ordered scannable volume) ─────────────

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("abdomen_ct4_postprocess_start")

        scan_dir = Path(working_dir) / "scan"
        scan_dir.mkdir(parents=True, exist_ok=True)

        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        arr = self._load_volume(inference_output["volume_path"])
        level, width = self._window()

        if arr.ndim < 3:
            qa_flags.append("not_a_volume")
            candidates: list[int] = [0] if arr.ndim == 2 else []
        else:
            if arr.shape[2] < int(self._cfg_qa.get("min_slices", 10)):
                qa_flags.append("insufficient_slices")
            candidates = self._candidate_slices(arr)

        # Render every candidate slice (one soft-tissue window) into working_dir/scan/,
        # keeping the ordered manifest the hook feeds to the engine. ``order`` (1-based
        # here) minus 1 is the engine's absolute slice index; ``z`` is the axial level.
        scan_slices: list[dict[str, Any]] = []
        for order, z in enumerate(candidates, 1):
            slice_2d = self._extract_slice(arr, z)
            if slice_2d is None:
                continue
            name = f"slc_{order:03d}_z{z:04d}.png"
            out_path = scan_dir / name
            if self._render_png(slice_2d, str(out_path), level, width):
                scan_slices.append({
                    "order": order - 1,   # 0-based absolute index == engine index
                    "z": int(z),
                    "name": name,
                    "local_path": str(out_path),
                })

        if not scan_slices:
            qa_flags.append("no_slices_rendered")

        # Preview: an evenly-spread subset always registered so the UI shows something
        # even when the engine is disabled / flags nothing. The hook appends flagged
        # slices before upload.
        preview_count = max(1, int(self._cfg_pre.get("preview_count", 6)))
        preview_orders = set(self._even_pick(list(range(len(scan_slices))), preview_count))
        artifacts: list[dict[str, Any]] = [
            {
                "name": s["name"],
                "artifact_type": "abdomen_ct4_slice_png",
                "local_path": s["local_path"],
                "content_type": "image/png",
            }
            for i, s in enumerate(scan_slices) if i in preview_orders
        ]

        image_dimensions = [int(d) for d in arr.shape]

        summary = {
            "modality": inference_output.get("modality"),
            "series_description": inference_output.get("series_description"),
            "study_description": inference_output.get("study_description"),
            "candidate_slices": len(candidates),
            "image_dimensions": image_dimensions,
            "hu_window": {"level": level, "width": width},
            "quantitative": False,
            "slice_selection": "ordered_full_volume",
            "inference_method": (
                "two_stage_multiagent (local Ollama: 4B multimodal triage over "
                "overlapping blocks → 27B text-only unified report)"
            ),
            # Filled by the task hook when the engine runs:
            "normal": None,
            "pathology_zones": [],
            "final_report": None,
            "processing_notes": (
                f"Rendered {len(candidates)} ordered soft-tissue axial slice(s) "
                f"(L{level:g}/W{width:g}). Stage 1 (4B multimodal MedGemma) triages "
                "overlapping slice blocks to flag pathology; Stage 2 (27B MedGemma, "
                "text-only) synthesizes the flagged blocks into one unified report, both "
                "served by the local Ollama daemon. NON-DIAGNOSTIC assistive output — "
                "full-volume radiologist review remains required."
            ),
        }

        logger.info(
            "abdomen_ct4_postprocess_complete",
            candidate_slices=len(candidates), scan_images=len(scan_slices),
            preview_artifacts=len(artifacts), image_dimensions=image_dimensions,
            qa_flags=qa_flags,
        )

        return {
            "summary": summary,
            "measurements": {
                "image_dimensions": image_dimensions,
                "candidate_slices": len(candidates),
                "scan_images_rendered": len(scan_slices),
            },
            "qa_flags": qa_flags,
            "qa_details": qa_details,
            "model_version": self._model_version,
            "model_checksum": self._model_checksum,
            "artifacts": artifacts,
            # Extra keys consumed by the Celery task hook only (not persisted):
            "scan_slices": scan_slices,
            "scan_config": {
                # Stage 1 triage model is HF (Settings); only Stage 2's Ollama report tag
                # lives here (falls back to global MEDGEMMA_MODEL in the hook when empty),
                # plus the engine batching knobs.
                "report_model": str(self._cfg_scan.get("report_model", "") or ""),
                "batch_size": int(self._cfg_scan.get("batch_size", 8)),
                "overlap": int(self._cfg_scan.get("overlap", 2)),
                "merge_gap": int(self._cfg_scan.get("merge_gap", 0)),
            },
        }

    # ── helpers ────────────────────────────────────────────────────────────────

    _DEFAULT_WINDOW = (40.0, 400.0)  # soft-tissue / portal-venous L40 W400

    def _window(self) -> tuple[float, float]:
        """The single soft-tissue HU window (level, width) used for every slice."""
        w = self._cfg_pre.get("window")
        if isinstance(w, dict) and "level" in w and "width" in w:
            return float(w["level"]), float(w["width"])
        return self._DEFAULT_WINDOW

    @staticmethod
    def _load_volume(volume_path: str) -> np.ndarray:
        """Load the NIfTI as a scalar float32 volume, collapsing any channel/time axis."""
        arr = np.squeeze(np.asarray(nib.load(volume_path).get_fdata(), dtype=np.float32))
        while arr.ndim > 3:
            arr = arr[..., 0]
        return arr

    def _trim_bounds(self, nz: int) -> tuple[int, int]:
        """Inclusive [lo, hi] z range after trimming partial-anatomy/table ends."""
        trim = float(self._cfg_pre.get("edge_trim_fraction", 0.05))
        lo = int(nz * trim)
        hi = int(nz * (1.0 - trim)) - 1
        if hi <= lo:
            lo, hi = 0, nz - 1
        return lo, hi

    def _candidate_slices(self, arr: np.ndarray) -> list[int]:
        """Foreground axial z-indices (superior→inferior), capped to max_scan_slices."""
        if arr.ndim < 3:
            return [0] if arr.ndim == 2 else []

        nz = int(arr.shape[2])
        lo, hi = self._trim_bounds(nz)
        min_fg = float(self._cfg_pre.get("min_foreground_fraction", 0.05))

        kept: list[int] = []
        for z in range(hi, lo - 1, -1):  # superior→inferior
            if float((arr[:, :, z] > -500.0).mean()) >= min_fg:
                kept.append(z)
        if not kept:  # degenerate (e.g. uncalibrated) — fall back to the trimmed range
            kept = list(range(hi, lo - 1, -1))

        cap = max(1, int(self._cfg_pre.get("max_scan_slices", 120)))
        if len(kept) > cap:
            picked = self._even_pick(kept, cap)
            logger.info("abdomen_ct4_candidates_strided", available=len(kept), scanned=len(picked))
            return picked
        return kept

    @staticmethod
    def _even_pick(items: list[int], k: int) -> list[int]:
        """Evenly-spread subset of ``items`` (order preserved), at most ``k`` elements."""
        if k <= 0 or not items:
            return []
        if len(items) <= k:
            return list(items)
        idx = np.linspace(0, len(items) - 1, k).round().astype(int)
        seen: list[int] = []
        for i in idx:
            v = items[int(i)]
            if v not in seen:
                seen.append(v)
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
        """Window a 2D CT slice to a fixed HU window and write a grayscale PNG."""
        try:
            from PIL import Image

            slice_2d = np.asarray(slice_2d, dtype=np.float32)
            if slice_2d.ndim != 2 or slice_2d.size == 0:
                return False

            lo = level - width / 2.0
            hi = level + width / 2.0
            norm = np.clip((slice_2d - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
            # Radiological display convention: transpose then flip the vertical axis
            # nibabel stores bottom-up so patient-anterior is up.
            img = Image.fromarray((np.flipud(norm.T) * 255.0).astype(np.uint8), mode="L")

            out_size = int(self._cfg_pre.get("out_size", 768) or 0)
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
            logger.warning("abdomen_ct4_render_failed", error=str(exc))
            return False
