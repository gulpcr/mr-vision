from __future__ import annotations

"""MRI (region) → MedGemma MULTIPARAMETRIC two-pass read.

Why this differs from the CT-report family: CT is a single acquisition, but MRI is
inherently MULTIPARAMETRIC — the same lesion looks different on T1, T2, FLAIR, DWI,
post-contrast T1 and SWI, and a radiologist *defines* a finding by its signal pattern
ACROSS sequences (an acute infarct only restricts on DWI; enhancement only shows on
post-contrast T1; blood blooms on SWI). Reading one sequence misses most brain/soft-tissue
pathology. So this pipeline:

  1. Classifies every series into a sequence (T1 / T1+C / T2 / FLAIR / DWI / SWI …) and
     downloads each present sequence.
  2. Co-registers them onto ONE reference grid. Same-session MR series share the DICOM
     frame of reference, so an affine resample (nibabel affines + scipy sampling) aligns
     them — the SAME voxel is the SAME anatomy across sequences. Series acquired in other
     planes are reformatted to the reference's canonical-axial grid.
  3. Renders ONE labeled PANEL MONTAGE per axial level: each tile is a sequence
     (percentile-windowed, no HU), captioned with its name. MedGemma sees every sequence
     of a level at once and compares signal.

The montage is treated as a single "window" so the SHARED Celery task hook, report
orchestration and report UI are reused unchanged. Pass 1 (scan the montages to flag
levels) and Pass 2 (report on the flagged montages) run in the hook. This module owns
series selection + co-registration + montage rendering only; it holds no infrastructure
imports. NON-DIAGNOSTIC assistive output — full-study radiologist review remains required.

Generic by design: ``USECASE_NAME`` comes from the directory; the sequence catalogue,
montage layout and windows live in ``model/inference_config.yaml``; region phrasing lives
in ``prompts.py`` (the ``REGION`` profile).
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
USECASE_NAME = USECASE_DIR.name
CONFIG_PATH = USECASE_DIR / "model" / "inference_config.yaml"

MONTAGE_WINDOW = "multi-sequence"

# MR series selection: skip single-plane localizers / scouts / derived maps.
_MR_SKIP_PATTERNS = [
    r"(?i)localizer", r"(?i)localiser", r"(?i)scout", r"(?i)survey",
    r"(?i)\bloc\b", r"(?i)3.?plane", r"(?i)tri.?planar", r"(?i)calibration",
    r"(?i)smart.?brain", r"(?i)\bmap\b", r"(?i)\bref\b",
    r"(?i)phoenix", r"(?i)report", r"(?i)view.?&.?go", r"(?i)\bmip\b", r"(?i)posdisp",
]


def _norm(text: str) -> str:
    """Normalize separators (``_ - .``) to spaces so ``\\b``-anchored sequence patterns
    match Siemens-style names (e.g. ``t1_se_tra``, ``t2_swi``, ``ep2d_diff_ADC``), which
    otherwise fail because ``_`` is a regex word character (no boundary at ``t1_``)."""
    return re.sub(r"[._\-]+", " ", str(text or "")).strip()


# Reading plane → canonical (RAS) slice axis (0 = R/L, 1 = A/P, 2 = S/I). Slicing along the
# axis gives the named 2-D plane: axial = fixed S/I, sagittal = fixed R/L, coronal = fixed A/P.
_PLANE_AXIS = {"axial": 2, "sagittal": 0, "coronal": 1}

# Series-description keywords used to prefer series ACQUIRED in a given plane (only applied
# when a region reads more than one plane, so each plane's montages use its native series).
_PLANE_KEYWORDS = {
    "axial": [r"(?i)\btra\b", r"(?i)\bax\b", r"(?i)axial", r"(?i)transvers"],
    "sagittal": [r"(?i)\bsag\b", r"(?i)sagittal"],
    "coronal": [r"(?i)\bcor\b", r"(?i)coronal"],
}


class Pipeline(BasePipeline):
    """Classifies + co-registers MR sequences and renders per-level panel montages."""

    def __init__(self) -> None:
        with open(CONFIG_PATH) as fh:
            self._cfg: dict[str, Any] = yaml.safe_load(fh) or {}
        self._cfg_pre = self._cfg.get("preprocessing", {})
        self._cfg_scan = self._cfg.get("scan", {})
        self._cfg_report = self._cfg.get("report", {})
        self._cfg_qa = self._cfg.get("quality_checks", {})
        self._model_version = f"{USECASE_NAME}_medgemma_v2.0.0"
        self._model_checksum = "n/a_no_model"

    # ── reading planes ─────────────────────────────────────────────────────────

    def _reading_planes(self) -> list[str]:
        """Planes this region reads. Defaults to a single axial plane (so every non-spine
        region is unchanged); a region can set ``reading_planes: [sagittal, axial]`` (spine)."""
        raw = self._cfg_pre.get("reading_planes")
        if isinstance(raw, list) and raw:
            planes = [str(p).lower() for p in raw if str(p).lower() in _PLANE_AXIS]
            return planes or ["axial"]
        one = str(self._cfg_pre.get("reading_plane", "axial")).lower()
        return [one if one in _PLANE_AXIS else "axial"]

    # ── series → sequence classification ──────────────────────────────────────

    def _sequence_catalogue(self) -> list[dict[str, Any]]:
        seqs = self._cfg_pre.get("sequences") or []
        return [s for s in seqs if isinstance(s, dict) and s.get("name") and s.get("patterns")]

    def _classify_sequences(
        self, series: list[Series], plane: str | None = None
    ) -> dict[str, Series]:
        """Map each series to a sequence label from the config catalogue.

        First matching sequence (catalogue order) wins per series; the largest series is
        kept per sequence. A series matching the ``base_t1`` sequence that also carries a
        contrast marker (``contrast_patterns``) is promoted to ``contrast_t1`` so a
        post-contrast T1 fills the enhancing panel instead of the plain-T1 panel.
        """
        catalogue = self._sequence_catalogue()
        contrast_patterns = self._cfg_pre.get("contrast_patterns") or []
        base_t1 = self._cfg_pre.get("base_t1")
        contrast_t1 = self._cfg_pre.get("contrast_t1")

        def _instances(s: Series) -> int:
            return getattr(s, "num_instances", 0) or 0

        def _is_skip(s: Series) -> bool:
            desc = _norm(s.series_description)
            return any(re.search(p, desc) for p in _MR_SKIP_PATTERNS) or (0 < _instances(s) <= 2)

        # When a region reads more than one plane, restrict to series ACQUIRED in this
        # plane (by description) so each plane's montages use its native series (e.g. spine
        # sagittal montages use sag T1/T2/STIR, axial montages use the axial T1/T2). If no
        # series matches the plane keyword, keep all (single-plane regions pass plane=None).
        pool = series
        if plane and plane in _PLANE_KEYWORDS:
            kws = _PLANE_KEYWORDS[plane]
            in_plane = [s for s in series if any(re.search(k, _norm(s.series_description)) for k in kws)]
            if in_plane:
                pool = in_plane

        chosen: dict[str, Series] = {}
        for s in pool:
            if _is_skip(s):
                continue
            protocol = getattr(s, "protocol_name", "") or ""
            combined = _norm(f"{s.series_description or ''} {protocol}")
            matched: str | None = None
            for entry in catalogue:
                if any(re.search(p, combined) for p in entry["patterns"]):
                    matched = str(entry["name"])
                    break
            if not matched:
                continue
            # Promote contrast-enhanced T1.
            if (
                base_t1 and contrast_t1 and matched == base_t1
                and any(re.search(p, combined) for p in contrast_patterns)
            ):
                matched = contrast_t1
            if matched not in chosen or _instances(s) > _instances(chosen[matched]):
                chosen[matched] = s
        return chosen

    def _fallback_series(self, series: list[Series]) -> Series | None:
        """Largest non-localizer (MR-preferred) series, for studies nothing classifies."""
        def _instances(s: Series) -> int:
            return getattr(s, "num_instances", 0) or 0

        def _is_skip(s: Series) -> bool:
            desc = _norm(s.series_description)
            return any(re.search(p, desc) for p in _MR_SKIP_PATTERNS) or (0 < _instances(s) <= 2)

        mr = [s for s in series if (s.modality or "").upper() in ("MR", "MRI") and not _is_skip(s)]
        pool = mr or [s for s in series if not _is_skip(s)] or list(series)
        return max(pool, key=_instances) if pool else None

    # ── Phase 1: preprocess (download each classified sequence) ────────────────

    def preprocess(
        self,
        study: Study,
        series: list[Series],
        working_dir: str,
        pacs: PACSClient,
        event_loop: Any = None,
    ) -> dict[str, Any]:
        loop = event_loop or asyncio.get_event_loop()

        nifti_dir = Path(working_dir) / "nifti"
        nifti_dir.mkdir(parents=True, exist_ok=True)

        qa_flags: list[str] = []
        qa_details: dict[str, Any] = {}
        planes = self._reading_planes()
        multi = len(planes) > 1

        # Classify + download the sequences for EACH reading plane. Downloads are de-duped by
        # series UID (a series shared across planes is fetched once). Single-plane regions
        # (planes == ["axial"]) behave exactly as before (plane=None → no plane restriction).
        downloaded: dict[str, str] = {}
        plane_volumes: dict[str, dict[str, str]] = {}
        plane_classified: dict[str, dict[str, str]] = {}
        for plane in planes:
            classified = self._classify_sequences(series, plane=plane if multi else None)
            vp: dict[str, str] = {}
            for seq_name, s in classified.items():
                uid = s.series_instance_uid
                if uid in downloaded:
                    vp[seq_name] = downloaded[uid]
                    continue
                out = str(nifti_dir / f"{self._slug(plane)}_{self._slug(seq_name)}.nii.gz")
                try:
                    loop.run_until_complete(
                        pacs.download_series_as_nifti(study.study_instance_uid, uid, out)
                    )
                    downloaded[uid] = out
                    vp[seq_name] = out
                except Exception as exc:
                    logger.warning(
                        f"{USECASE_NAME}_sequence_download_failed", plane=plane, seq=seq_name, error=str(exc)
                    )
            if vp:
                plane_volumes[plane] = vp
                plane_classified[plane] = {k: v.series_description for k, v in classified.items()}

        # Nothing classified in any plane — fall back to the largest series as a single panel.
        if not plane_volumes:
            fb = self._fallback_series(series)
            if fb is None:
                raise ValueError(f"No series found for {USECASE_NAME} pipeline")
            out = str(nifti_dir / "primary.nii.gz")
            loop.run_until_complete(
                pacs.download_series_as_nifti(study.study_instance_uid, fb.series_instance_uid, out)
            )
            plane_volumes[planes[0]] = {"MR": out}
            qa_flags.append("series_region_unmatched")
            qa_details["series_region_unmatched"] = (
                f"No configured sequence matched; read '{fb.series_description}' as a single panel."
            )

        prim = next(iter(plane_volumes))
        if len(plane_volumes[prim]) == 1 and "series_region_unmatched" not in qa_flags:
            qa_flags.append("single_sequence")
            qa_details["single_sequence"] = (
                f"Only one MR sequence was identified in the {prim} plane; multiparametric "
                "comparison is limited."
            )

        logger.info(
            f"{USECASE_NAME}_preprocess_complete",
            study_uid=study.study_instance_uid,
            planes=list(plane_volumes.keys()),
            classified=plane_classified,
        )

        return {
            "plane_volumes": plane_volumes,
            "study_uid": study.study_instance_uid,
            "modality": (study.modality or "MR").upper() or "MR",
            "study_description": study.study_description,
            "qa_flags": qa_flags,
            "qa_details": qa_details,
        }

    def _pick_reference(self, volume_paths: dict[str, str], slice_axis: int = 2) -> str:
        # Choose the reference that yields the BEST reformats IN THE READING PLANE: the largest
        # in-plane sampling (the two axes other than the slice axis) after canonical
        # reorientation. This deprioritizes series acquired in a different plane / thin-plane
        # (e.g. a 4 mm coronal FLAIR would give degenerate strip-like axial slices), so the
        # reference is a series natively sampled in the reading plane. reference_preference only
        # breaks ties between comparable series.
        inplane_axes = [a for a in (0, 1, 2) if a != slice_axis]
        pref = [str(x) for x in (self._cfg_pre.get("reference_preference") or [])]
        scored: list[tuple[int, int, str]] = []
        for seq, path in volume_paths.items():
            try:
                sh = [int(d) for d in nib.as_closest_canonical(nib.load(path)).shape[:3]]
                while len(sh) < 3:
                    sh.append(1)
                # in-plane pixels; require a few slices along the slice axis
                inplane = sh[inplane_axes[0]] * sh[inplane_axes[1]] if sh[slice_axis] >= 3 else 0
            except Exception:
                inplane = 0
            pref_rank = pref.index(seq) if seq in pref else len(pref)
            scored.append((inplane, -pref_rank, seq))
        scored.sort(reverse=True)
        return scored[0][2] if scored else next(iter(volume_paths))

    # ── Phase 2: infer (no learned model — pass-through) ─────────────────────────

    def infer(self, preprocessed: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info(f"{USECASE_NAME}_infer_passthrough")
        return dict(preprocessed)

    # ── Phase 3: postprocess (co-register + render per-level montages) ───────────

    def postprocess(self, inference_output: dict[str, Any], working_dir: str) -> dict[str, Any]:
        logger.info(f"{USECASE_NAME}_postprocess_start")

        scan_dir = Path(working_dir) / "scan"
        scan_dir.mkdir(parents=True, exist_ok=True)

        qa_flags: list[str] = list(inference_output.get("qa_flags", []))
        qa_details: dict[str, Any] = dict(inference_output.get("qa_details", {}))

        plane_volumes: dict[str, dict[str, str]] = inference_output["plane_volumes"]
        planes = list(plane_volumes.keys())
        multi = len(planes) > 1

        # Render each reading plane's montages (window = plane when >1 plane). z is offset per
        # plane so sagittal/axial slice indices never collide in the shared report grouping.
        scan_slices: list[dict[str, Any]] = []
        planes_info: dict[str, Any] = {}
        scan_windows: list[str] = []
        for p_i, plane in enumerate(planes):
            info = self._render_plane(plane, plane_volumes[plane], scan_dir, p_i * 100000, multi)
            if info is None:
                continue
            scan_slices.extend(info["scan_slices"])
            planes_info[plane] = info
            if info["window"] not in scan_windows:
                scan_windows.append(info["window"])

        if not scan_slices:
            qa_flags.append("no_slices_rendered")
        prim = planes[0] if planes else None
        prim_info = planes_info.get(prim, {}) if prim else {}
        if prim_info and 0 < prim_info.get("n_candidates", 0) < int(self._cfg_qa.get("min_slices", 10)):
            qa_flags.append("insufficient_slices")

        # Preview artifacts: an even spread across all rendered montages (all planes).
        preview_count = max(1, int(self._cfg_pre.get("preview_count", 6)))
        artifacts: list[dict[str, Any]] = []
        if scan_slices:
            idxs = np.linspace(0, len(scan_slices) - 1, min(preview_count, len(scan_slices))).round().astype(int)
            for i in sorted(set(int(x) for x in idxs)):
                e = scan_slices[i]
                artifacts.append({
                    "name": e["name"],
                    "artifact_type": f"{USECASE_NAME}_slice_png",
                    "local_path": e["local_path"],
                    "content_type": "image/png",
                })

        # Union of sequences used across planes (in montage order); primary plane ref + dims.
        sequences_used: list[str] = []
        for plane in planes:
            for s in (planes_info.get(plane, {}).get("panel_order") or []):
                if s not in sequences_used:
                    sequences_used.append(s)
        image_dimensions = prim_info.get("dims", [])
        total_candidates = sum(i["n_candidates"] for i in planes_info.values())
        report_windows = list(scan_windows)
        planes_desc = "; ".join(
            f"{p} [{', '.join(planes_info[p]['panel_order'])}] (ref {planes_info[p]['ref_seq']})"
            for p in planes if p in planes_info
        )

        summary = {
            "modality": inference_output.get("modality"),
            "series_description": prim_info.get("ref_seq"),
            "study_description": inference_output.get("study_description"),
            "candidate_slices": total_candidates,
            "image_dimensions": image_dimensions,
            "sequences_used": sequences_used,
            "reading_planes": planes,
            "reference_sequence": prim_info.get("ref_seq"),
            "hu_windows": scan_windows,              # kept key name for UI parity
            "scan_windows": scan_windows,
            "report_windows": report_windows,
            "quantitative": False,
            "slice_selection": "medgemma_full_volume_scan",
            "inference_method": (
                "medgemma_multiparametric_two_pass "
                "(co-registered sequence montages → scan → report flagged levels)"
            ),
            "anomaly_slices": [],
            "processing_notes": (
                f"Rendered {total_candidates} montage(s) across plane(s) — {planes_desc}; per-sequence "
                "percentile windows. MedGemma scans every montage to flag potential abnormalities by "
                "their cross-sequence signal pattern, then reports on the flagged ones. NON-DIAGNOSTIC "
                "assistive output — full radiologist review remains required."
            ),
        }

        logger.info(
            f"{USECASE_NAME}_postprocess_complete",
            planes=planes, montages=len(scan_slices), sequences=sequences_used,
            image_dimensions=image_dimensions, qa_flags=qa_flags,
        )

        return {
            "summary": summary,
            "measurements": {
                "image_dimensions": image_dimensions,
                "candidate_slices": total_candidates,
                "scan_images_rendered": len(scan_slices),
                "sequences_used": sequences_used,
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
                "max_report_levels": int(self._cfg_report.get("max_report_levels", 10)),
                "model": str(self._cfg_scan.get("model", "") or ""),
                "measure": self._cfg.get("measurement", {}) or {},
            },
        }

    def _render_plane(
        self,
        plane: str,
        volume_paths: dict[str, str],
        scan_dir: Path,
        z_offset: int,
        multi: bool,
    ) -> dict[str, Any] | None:
        """Co-register this plane's sequences and render one labeled montage per slice.

        Slices along the plane's canonical axis; ``window`` is the plane name when the region
        reads several planes (so the shared hook scans each plane independently), else the
        legacy ``multi-sequence``. Returns per-plane info, or None if the reference won't load.
        """
        axis = _PLANE_AXIS.get(plane, 2)
        ref_seq = self._pick_reference(volume_paths, axis)
        try:
            ref_img = self._load_canonical(volume_paths[ref_seq])
            ref_arr = np.squeeze(np.asarray(ref_img.get_fdata(), dtype=np.float32))
        except Exception as exc:
            logger.warning(f"{USECASE_NAME}_reference_load_failed", plane=plane, error=str(exc))
            return None
        while ref_arr.ndim > 3:
            ref_arr = ref_arr[..., 0]

        order = self._display_order(list(volume_paths.keys()))
        seq_arrays: dict[str, np.ndarray] = {}
        seq_bounds: dict[str, tuple[float, float]] = {}
        for seq in order:
            arr = ref_arr if seq == ref_seq else self._resample_to_ref(volume_paths[seq], ref_img)
            if arr is None:
                continue
            seq_arrays[seq] = arr
            seq_bounds[seq] = self._window_bounds(arr)
        panel_order = [s for s in order if s in seq_arrays]

        ref_lo, ref_hi = seq_bounds.get(ref_seq, self._window_bounds(ref_arr))
        if ref_arr.ndim < 3:
            candidates = [0] if ref_arr.ndim == 2 else []
        else:
            candidates = self._candidate_slices(ref_arr, ref_lo, ref_hi, axis)

        window = plane if multi else MONTAGE_WINDOW
        scan_slices: list[dict[str, Any]] = []
        for order_i, sidx in enumerate(candidates, 1):
            z = z_offset + int(sidx)
            name = f"{self._slug(window)}_{order_i:03d}_z{z:05d}_montage.png"
            out_path = scan_dir / name
            if self._render_montage(seq_arrays, seq_bounds, panel_order, int(sidx), str(out_path), axis):
                scan_slices.append({
                    "z": z,
                    "window": window,
                    "name": name,
                    "local_path": str(out_path),
                    "order": order_i,
                    "plane": plane,
                })

        return {
            "scan_slices": scan_slices,
            "panel_order": panel_order,
            "ref_seq": ref_seq,
            "window": window,
            "dims": [int(d) for d in ref_arr.shape],
            "n_candidates": len(candidates),
        }

    # ── sequence ordering / resampling ─────────────────────────────────────────

    def _display_order(self, present: list[str]) -> list[str]:
        """Present sequences in the configured montage order (unknowns appended)."""
        order = [str(x) for x in (self._cfg_pre.get("sequence_order") or [])]
        ordered = [s for s in order if s in present]
        ordered += [s for s in present if s not in ordered]
        return ordered

    def _resample_to_ref(self, moving_path: str, ref_img: Any) -> np.ndarray | None:
        """Resample a moving sequence onto the reference grid via the DICOM affines.

        Same-session MR series share the frame of reference, so mapping reference voxels →
        world (reference affine) → moving voxels (inverse moving affine) and linearly
        sampling aligns them without a registration algorithm. Volumes acquired in other
        planes are thereby reformatted to the reference's canonical-axial grid.
        """
        try:
            from scipy.ndimage import map_coordinates

            mov = nib.load(moving_path)
            mov_arr = np.squeeze(np.asarray(mov.get_fdata(), dtype=np.float32))
            while mov_arr.ndim > 3:
                mov_arr = mov_arr[..., 0]
            if mov_arr.ndim != 3:
                return None

            ref_shape = tuple(int(d) for d in ref_img.shape[:3])
            # Affine mapping reference voxel indices → moving voxel indices.
            m = np.linalg.inv(mov.affine) @ ref_img.affine
            ii, jj, kk = np.meshgrid(
                np.arange(ref_shape[0], dtype=np.float32),
                np.arange(ref_shape[1], dtype=np.float32),
                np.arange(ref_shape[2], dtype=np.float32),
                indexing="ij",
            )
            flat = np.stack([ii.ravel(), jj.ravel(), kk.ravel(), np.ones(ii.size, np.float32)], axis=0)
            mov_vox = m @ flat  # 4 x N
            sampled = map_coordinates(mov_arr, mov_vox[:3], order=1, mode="constant", cval=0.0)
            return sampled.reshape(ref_shape).astype(np.float32)
        except Exception as exc:
            logger.warning(f"{USECASE_NAME}_resample_failed", moving=moving_path, error=str(exc))
            return None

    # ── montage rendering ──────────────────────────────────────────────────────

    def _render_montage(
        self,
        seq_arrays: dict[str, np.ndarray],
        seq_bounds: dict[str, tuple[float, float]],
        panel_order: list[str],
        z: int,
        out_path: str,
        axis: int = 2,
    ) -> bool:
        """Render a labeled grid of the sequence tiles at slice ``z`` along ``axis``."""
        try:
            from PIL import Image, ImageDraw, ImageFont

            tile = int(self._cfg_pre.get("tile_size", 448) or 448)
            cols = max(1, int(self._cfg_pre.get("montage_cols", 3)))
            label_h = max(16, tile // 16)

            tiles: list[tuple[str, Image.Image]] = []
            for seq in panel_order:
                arr = seq_arrays.get(seq)
                if arr is None:
                    continue
                sl = self._extract_slice(arr, z, axis)  # already display-oriented
                if sl is None:
                    continue
                lo, hi = seq_bounds.get(seq, (0.0, 1.0))
                norm = np.clip((np.asarray(sl, np.float32) - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
                img = Image.fromarray((norm * 255.0).astype(np.uint8), mode="L").convert("RGB")
                # Scale the long edge to the tile size (UP or down) so anatomy fills the
                # tile, then letterbox the short edge into a square, preserving aspect.
                w, h = img.size
                scale = tile / max(w, h) if max(w, h) else 1.0
                img = img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.BILINEAR)
                canvas = Image.new("RGB", (tile, tile), (0, 0, 0))
                canvas.paste(img, ((tile - img.size[0]) // 2, (tile - img.size[1]) // 2))
                tiles.append((seq, canvas))

            if not tiles:
                return False

            n = len(tiles)
            ncols = min(cols, n)
            nrows = (n + ncols - 1) // ncols
            cell_w, cell_h = tile, tile + label_h
            montage = Image.new("RGB", (ncols * cell_w, nrows * cell_h), (0, 0, 0))
            draw = ImageDraw.Draw(montage)
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(12, label_h - 6))
            except Exception:
                font = ImageFont.load_default()

            for idx, (seq, timg) in enumerate(tiles):
                r, c = divmod(idx, ncols)
                x0, y0 = c * cell_w, r * cell_h
                # Label bar (black) with the sequence name in white.
                draw.rectangle([x0, y0, x0 + cell_w, y0 + label_h], fill=(0, 0, 0))
                draw.text((x0 + 4, y0 + 1), seq, fill=(255, 255, 255), font=font)
                montage.paste(timg, (x0, y0 + label_h))

            max_size = int(self._cfg_pre.get("montage_max_size", 1536) or 0)
            long_edge = max(montage.size)
            if max_size and long_edge > max_size:
                scale = max_size / long_edge
                montage = montage.resize(
                    (max(1, int(montage.size[0] * scale)), max(1, int(montage.size[1] * scale))),
                    Image.BILINEAR,
                )
            montage.save(out_path, format="PNG")
            return True
        except Exception as exc:
            logger.warning(f"{USECASE_NAME}_montage_failed", error=str(exc))
            return False

    # ── volume / slice helpers ─────────────────────────────────────────────────

    @staticmethod
    def _slug(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "mr"

    @staticmethod
    def _load_canonical(volume_path: str) -> Any:
        """Load NIfTI reoriented to closest-canonical (RAS) — axial along the last axis."""
        img = nib.load(volume_path)
        try:
            img = nib.as_closest_canonical(img)
        except Exception as exc:
            logger.warning(f"{USECASE_NAME}_canonical_reorient_failed", error=str(exc))
        return img

    def _window_bounds(self, arr: np.ndarray) -> tuple[float, float]:
        """Robust per-sequence intensity window from percentiles of the tissue voxels."""
        finite = arr[np.isfinite(arr)]
        tissue = finite[finite > 0]
        if tissue.size < 100:
            tissue = finite
        if tissue.size == 0:
            return 0.0, 1.0
        lo = float(np.percentile(tissue, float(self._cfg_pre.get("window_low_pct", 1.0))))
        hi = float(np.percentile(tissue, float(self._cfg_pre.get("window_high_pct", 99.0))))
        if hi <= lo:
            lo, hi = float(tissue.min()), float(tissue.max())
            if hi <= lo:
                hi = lo + 1.0
        return lo, hi

    def _trim_bounds(self, nz: int) -> tuple[int, int]:
        trim = float(self._cfg_pre.get("edge_trim_fraction", 0.05))
        lo = int(nz * trim)
        hi = int(nz * (1.0 - trim)) - 1
        if hi <= lo:
            lo, hi = 0, nz - 1
        return lo, hi

    def _candidate_slices(self, arr: np.ndarray, lo: float, hi: float, axis: int = 2) -> list[int]:
        """Foreground slice indices along ``axis`` (descending), capped to max_scan_slices."""
        if arr.ndim < 3:
            return [0] if arr.ndim == 2 else []

        nz = int(arr.shape[axis])
        lo_b, hi_b = self._trim_bounds(nz)
        min_fg = float(self._cfg_pre.get("min_foreground_fraction", 0.05))
        floor = lo + 0.02 * (hi - lo)

        kept: list[int] = []
        for z in range(hi_b, lo_b - 1, -1):
            sl = np.take(arr, z, axis=axis)
            if float((sl > floor).mean()) >= min_fg:
                kept.append(z)
        if not kept:
            kept = list(range(hi_b, lo_b - 1, -1))

        cap = max(1, int(self._cfg_pre.get("max_scan_slices", 96)))
        if len(kept) > cap:
            picked = self._even_pick(kept, cap)
            logger.info(f"{USECASE_NAME}_candidates_strided", available=len(kept), scanned=len(picked))
            return picked
        return kept

    @staticmethod
    def _even_pick(items: list[int], k: int) -> list[int]:
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
    def _extract_slice(arr: np.ndarray, idx: int, axis: int = 2) -> np.ndarray | None:
        """Display-oriented 2-D slice at ``idx`` along ``axis`` (superior/anterior up).

        ``np.flipud(sl.T)`` yields the radiological-ish orientation for axial (fixed S/I),
        sagittal (fixed R/L) and coronal (fixed A/P) alike — superior/anterior toward the top.
        """
        if arr.ndim == 2:
            sl = arr
        elif arr.ndim == 3 and 0 <= idx < arr.shape[axis]:
            sl = np.take(arr, idx, axis=axis)
        else:
            return None
        return np.flipud(np.asarray(sl, dtype=np.float32).T)
