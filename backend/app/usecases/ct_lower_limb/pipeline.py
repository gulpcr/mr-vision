from __future__ import annotations

"""Abdomen CT (v2) → MedGemma two-pass pipeline.

Unlike ``ct_lower_limb`` — which samples a handful of axial levels (evenly or via a
VQVAE anomaly selector) and hands them straight to MedGemma — ``ct_lower_limb`` does
NOT pre-select the slices to report. Instead it renders the WHOLE diagnostic volume
and lets MedGemma itself decide what is abnormal, in two passes driven by the Celery
task hook (infrastructure/queue/tasks.py):

  Pass 1 (scan):   every foreground axial slice is rendered and fed to MedGemma in
                   batches until the entire volume has been reviewed; MedGemma flags
                   the slices (z-levels) that look potentially abnormal.
  Pass 2 (report): only the flagged slices are sent back to MedGemma, which writes a
                   structured free-text findings/impression report on them.

This module owns Pass-0 only: series selection + rendering the full set of scannable
slices. It stays free of infrastructure imports (the MedGemma calls live in the task
hook, using ``app.usecases.ct_lower_limb.report`` + ``prompts``). The rendered slices
are written to ``working_dir/scan/`` and described by a ``scan_slices`` manifest
returned to the hook; only a small preview subset is registered as artifacts here —
the hook appends the flagged slices as artifacts before the MinIO upload, so exactly
what MedGemma reported on is what gets stored/shown.

Design choices mirror ct_lower_limb: fixed HU windowing (not percentile — it flattens
lesion-vs-parenchyma contrast), radiological display orientation, and NON-DIAGNOSTIC
assistive output. The trade-off vs ct_lower_limb is cost: scanning the whole volume is
many more MedGemma calls, bounded by ``max_scan_slices``/``batch_size`` in config.
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
    """Renders the full scannable abdomen CT volume; MedGemma flags + narrates later."""

    def __init__(self) -> None:
        with open(CONFIG_PATH) as fh:
            self._cfg: dict[str, Any] = yaml.safe_load(fh) or {}
        self._cfg_pre = self._cfg.get("preprocessing", {})
        self._cfg_scan = self._cfg.get("scan", {})
        self._cfg_report = self._cfg.get("report", {})
        self._cfg_qa = self._cfg.get("quality_checks", {})
        self._model_version = "ct_lower_limb_medgemma_v1.0.0"
        self._model_checksum = "n/a_no_model"

    # ── series selection ─────────────────────────────────────────────────────

    def _select_primary_series(self, series: list[Series]) -> Series | None:
        """Pick the REGION-appropriate diagnostic CT volume.

        Among non-scout CT series, prefer those whose description matches this region's
        ``preprocessing.series_patterns`` — so on a multi-series study (e.g. a separate
        chest series AND an abdomen series in one study) each region-plugin reads ITS
        OWN series rather than just the largest one. Ties broken by instance count.
        Falls back to the largest non-scout series when nothing matches, setting
        ``self._series_region_matched=False`` so ``preprocess`` can raise a QA flag.
        """
        self._series_region_matched = True
        if not series:
            return None

        def _instances(s: Series) -> int:
            return getattr(s, "num_instances", 0) or 0

        def _is_scout(s: Series) -> bool:
            desc = (s.series_description or "").strip()
            n = _instances(s)
            return any(re.search(p, desc) for p in _CT_SCOUT_PATTERNS) or (0 < n <= 2)

        ct = [s for s in series if (s.modality or "").upper() == "CT" and not _is_scout(s)]
        pool = ct or [s for s in series if not _is_scout(s)] or list(series)

        # Region match is TAG-FIRST, description-fallback:
        #   1. DICOM BodyPartExamined (0018,0015) — the canonical, reliable per-series
        #      body-region tag — matched against this region's `body_part_tags`.
        #   2. SeriesDescription regex (`series_patterns`) — the fallback, because
        #      BodyPartExamined is DICOM Type 3 (optional) and is frequently blank
        #      (it is empty on this deployment's scanners), so the free-text description
        #      is often the only populated region signal.
        tags = [t.strip().upper() for t in (self._cfg_pre.get("body_part_tags") or [])]
        patterns = self._cfg_pre.get("series_patterns") or []

        def _bpe(s: Series) -> str:
            return (s.body_part_examined or "").strip().upper().replace(" ", "")

        if tags:
            tag_hits = [
                s for s in pool
                if _bpe(s) and any(_bpe(s) == t or t in _bpe(s) for t in tags)
            ]
            if tag_hits:
                return max(tag_hits, key=_instances)
        if patterns:
            desc_hits = [
                s for s in pool
                if any(re.search(p, s.series_description or "") for p in patterns)
            ]
            if desc_hits:
                return max(desc_hits, key=_instances)
        # Neither the tag nor the description identified this region — read the largest
        # series but flag it, so the report notes it may not match this region's anatomy.
        if tags or patterns:
            self._series_region_matched = False
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
            raise ValueError("No series found for ct_lower_limb pipeline")

        nifti_dir = Path(working_dir) / "nifti"
        nifti_dir.mkdir(parents=True, exist_ok=True)
        volume_path = str(nifti_dir / "volume.nii.gz")

        loop.run_until_complete(
            pacs.download_series_as_nifti(
                study.study_instance_uid, primary.series_instance_uid, volume_path
            )
        )

        qa_flags: list[str] = []
        qa_details: dict[str, Any] = {}
        if not getattr(self, "_series_region_matched", True):
            # No series matched this region's series_patterns on a study that has other
            # series — this plugin read the largest series, which may be another region.
            qa_flags.append("series_region_unmatched")
            qa_details["series_region_unmatched"] = (
                f"No series matched this region; read '{primary.series_description}'."
            )

        logger.info(
            "ct_lower_limb_preprocess_complete",
            study_uid=study.study_instance_uid,
            series=primary.series_description,
            modality=primary.modality,
            instances=getattr(primary, "num_instances", None),
            region_matched=getattr(self, "_series_region_matched", True),
        )

        return {
            "volume_path": volume_path,
            "study_uid": study.study_instance_uid,
            "modality": (primary.modality or study.modality or "").upper() or None,
            "series_description": primary.series_description,
            "study_description": study.study_description,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
        }

    # ── Phase 2: infer (no learned model — pass-through) ─────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        # The "inference" for ct_lower_limb is the MedGemma two-pass scan/report, which
        # runs in the Celery task hook (it needs the infrastructure MedGemma client).
        # Here we only pass the preprocessed context through.
        logger.info("ct_lower_limb_infer_passthrough")
        return dict(preprocessed)

    # ── Phase 3: postprocess (render the full scannable volume) ──────────────────

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info("ct_lower_limb_postprocess_start")

        scan_dir = Path(working_dir) / "scan"
        scan_dir.mkdir(parents=True, exist_ok=True)

        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        arr = self._load_volume(inference_output["volume_path"])
        windows = self._windows()
        win_names = [str(w["name"]) for w in windows]

        # scan_windows / report_windows are validated against the rendered windows.
        scan_windows = [w for w in (self._cfg_scan.get("scan_windows") or []) if w in win_names]
        if not scan_windows:
            # Back-compat: accept a single scan_window, else default to the first window.
            legacy = str(self._cfg_scan.get("scan_window", win_names[0]))
            scan_windows = [legacy if legacy in win_names else win_names[0]]
        report_windows = [w for w in (self._cfg_report.get("windows") or []) if w in win_names]
        if not report_windows:
            report_windows = [scan_windows[0]]

        if arr.ndim < 3:
            qa_flags.append("not_a_volume")
            candidates: list[int] = [0] if arr.ndim == 2 else []
        else:
            if arr.shape[2] < int(self._cfg_qa.get("min_slices", 10)):
                qa_flags.append("insufficient_slices")
            candidates = self._candidate_slices(arr)

        # Render every candidate slice in every configured window into working_dir/scan/.
        # scan_slices is the manifest the task hook reads (it is NOT persisted to the
        # result — the task only saves the known result keys).
        scan_slices: list[dict[str, Any]] = []
        for order, z in enumerate(candidates, 1):
            slice_2d = self._extract_slice(arr, z)
            if slice_2d is None:
                continue
            for w in windows:
                name = f"slc_{order:03d}_z{z:04d}_{self._slug(w['name'])}.png"
                out_path = scan_dir / name
                if self._render_png(slice_2d, str(out_path), float(w["level"]), float(w["width"])):
                    scan_slices.append({
                        "z": int(z),
                        "window": str(w["name"]),
                        "name": name,
                        "local_path": str(out_path),
                        "order": order,
                    })

        if not scan_slices:
            qa_flags.append("no_slices_rendered")

        # Preview: a small, evenly-spread set of scan-window slices always registered as
        # artifacts so the UI shows something even when MedGemma is off / flags nothing.
        # The hook appends the flagged slices to this list before upload.
        preview_count = max(1, int(self._cfg_pre.get("preview_count", 6)))
        preview_z = self._even_pick(candidates, preview_count)
        by_z_window = {(s["z"], s["window"]): s for s in scan_slices}
        artifacts: list[dict[str, Any]] = []
        for z in preview_z:
            entry = by_z_window.get((int(z), scan_windows[0]))
            if entry:
                artifacts.append({
                    "name": entry["name"],
                    "artifact_type": "ct_lower_limb_slice_png",
                    "local_path": entry["local_path"],
                    "content_type": "image/png",
                })

        image_dimensions = [int(d) for d in arr.shape]
        window_desc = ", ".join(
            f"{w['name']} (L{float(w['level']):g}/W{float(w['width']):g})" for w in windows
        )

        summary = {
            "modality": inference_output.get("modality"),
            "series_description": inference_output.get("series_description"),
            "study_description": inference_output.get("study_description"),
            "candidate_slices": len(candidates),
            "image_dimensions": image_dimensions,
            "hu_windows": win_names,
            "scan_windows": scan_windows,
            "report_windows": report_windows,
            "quantitative": False,
            "slice_selection": "medgemma_full_volume_scan",
            "inference_method": "medgemma_two_pass (scan whole volume → report flagged slices)",
            # Filled by the task hook when MedGemma runs:
            "anomaly_slices": [],
            "processing_notes": (
                f"Rendered {len(candidates)} foreground axial slice(s) across the volume in "
                f"{len(windows)} HU window(s): {window_desc}. MedGemma scans EVERY slice in "
                f"batches to flag potential abnormalities, then reports on the flagged slices "
                "only. NON-DIAGNOSTIC assistive output: intervening/non-flagged slices are not "
                "individually reported and no measurement/segmentation is performed — full-volume "
                "radiologist review remains required."
            ),
        }

        logger.info(
            "ct_lower_limb_postprocess_complete",
            candidate_slices=len(candidates), scan_images=len(scan_slices),
            preview_artifacts=len(artifacts), scan_windows=scan_windows,
            report_windows=report_windows, image_dimensions=image_dimensions, qa_flags=qa_flags,
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
                "scan_windows": scan_windows,
                "report_windows": report_windows,
                "batch_size": int(self._cfg_scan.get("batch_size", 2)),
                "max_report_levels": int(self._cfg_report.get("max_report_levels", 6)),
                # Per-use-case model override (falls back to global MEDGEMMA_MODEL in
                # the hook when empty).
                "model": str(self._cfg_scan.get("model", "") or ""),
                # SAM-Med3D tumour-measurement config (measurement: block).
                "measure": self._cfg.get("measurement", {}) or {},
            },
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
        trim = float(self._cfg_pre.get("edge_trim_fraction", 0.05))
        lo = int(nz * trim)
        hi = int(nz * (1.0 - trim)) - 1
        if hi <= lo:
            lo, hi = 0, nz - 1
        return lo, hi

    def _candidate_slices(self, arr: np.ndarray) -> list[int]:
        """Foreground axial z-indices (superior→inferior), capped to max_scan_slices.

        Trims the partial-anatomy ends, drops near-empty (air/table) slices, and — if
        the surviving count exceeds the cap — keeps an evenly-strided subset so the
        whole volume is still covered. Returned descending (superior→inferior), which
        is the radiological reading order used throughout the plugin.
        """
        if arr.ndim < 3:
            return [0] if arr.ndim == 2 else []

        nz = int(arr.shape[2])
        lo, hi = self._trim_bounds(nz)
        min_fg = float(self._cfg_pre.get("min_foreground_fraction", 0.05))

        kept: list[int] = []
        for z in range(hi, lo - 1, -1):  # superior→inferior
            sl = arr[:, :, z]
            if float((sl > -500.0).mean()) >= min_fg:
                kept.append(z)
        if not kept:  # degenerate (e.g. uncalibrated) — fall back to the trimmed range
            kept = list(range(hi, lo - 1, -1))

        cap = max(1, int(self._cfg_pre.get("max_scan_slices", 48)))
        if len(kept) > cap:
            picked = self._even_pick(kept, cap)
            logger.info("ct_lower_limb_candidates_strided", available=len(kept), scanned=len(picked))
            return picked
        return kept

    @staticmethod
    def _even_pick(items: list[int], k: int) -> list[int]:
        """Evenly-spread subset of ``items`` (preserving order), at most ``k`` elements."""
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
            logger.warning("ct_lower_limb_render_failed", error=str(exc))
            return False
