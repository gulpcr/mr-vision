"""Display-time correction for report-family slice tiles rendered before the orientation fix.

Until the pipelines recorded ``summary.tile_orientation = "dicom"``, tiles were stored in
the wrong orientation:

* CT (``abdomen_ct`` + ``ct_*``) — upside down: an extra vertical flip on a volume that was
  never reoriented. Undone exactly by flipping the image vertically.
* MRI (``*_mri``) — each sequence panel mirrored left↔right (the patient's right on image
  right, anterior on image right for sagittal). The montage cannot simply be mirrored as a
  whole — that would also mirror the sequence labels and reverse the panel order — so each
  panel's image area is mirrored in place, below its label bar. The panel grid is
  reconstructed from the image size and the plugin's layout settings (``tile_size``,
  ``montage_cols``, ``montage_max_size``) exactly as ``pipeline._render_montage`` built it;
  if no layout matches the image size, the tile is returned unchanged.
"""
from __future__ import annotations

import io
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.application.ct_report_regions import CT_REPORT_USECASES

logger = structlog.get_logger(__name__)

CURRENT_ORIENTATION = "dicom"
_TILE_NAME_RE = re.compile(r"(^slc_\d+_z\d+_.+|_\d+_z\d+_montage)\.png$")
_USECASES_DIR = Path(__file__).resolve().parent.parent / "usecases"
_MAX_PANELS = 16
_SIZE_TOLERANCE_PX = 2


def needs_correction(usecase: str, artifact_name: str, summary: dict[str, Any] | None) -> bool:
    """Whether this stored artifact is a report-family tile from before the fix."""
    if usecase not in CT_REPORT_USECASES:
        return False
    if not _TILE_NAME_RE.search(artifact_name.rsplit("/", 1)[-1]):
        return False
    return (summary or {}).get("tile_orientation") != CURRENT_ORIENTATION


@lru_cache(maxsize=32)
def _montage_settings(usecase: str) -> tuple[int, int, int]:
    path = _USECASES_DIR / usecase / "model" / "inference_config.yaml"
    try:
        pre = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("preprocessing") or {}
    except (OSError, yaml.YAMLError):
        pre = {}
    return (
        int(pre.get("tile_size", 448) or 448),
        max(1, int(pre.get("montage_cols", 3))),
        int(pre.get("montage_max_size", 1536) or 0),
    )


def _montage_grid(size: tuple[int, int], usecase: str) -> tuple[int, int, float, int, int] | None:
    """(cols, rows, scale, tile, label_h) of the montage that has this image size."""
    tile, cols, max_size = _montage_settings(usecase)
    label_h = max(16, tile // 16)  # as pipeline._render_montage
    seen: set[tuple[int, int]] = set()
    for n in range(1, _MAX_PANELS + 1):
        ncols = min(cols, n)
        nrows = math.ceil(n / ncols)
        if (ncols, nrows) in seen:
            continue
        seen.add((ncols, nrows))
        full_w, full_h = ncols * tile, nrows * (tile + label_h)
        scale = 1.0
        if max_size and max(full_w, full_h) > max_size:
            scale = max_size / max(full_w, full_h)
        want = (max(1, int(full_w * scale)), max(1, int(full_h * scale)))
        if all(abs(a - b) <= _SIZE_TOLERANCE_PX for a, b in zip(size, want)):
            return ncols, nrows, scale, tile, label_h
    return None


def correct_tile(data: bytes, usecase: str) -> bytes:
    """Return the PNG re-oriented to the DICOM viewer's orientation (or unchanged)."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if usecase.endswith("_mri"):
            grid = _montage_grid(img.size, usecase)
            if grid is None:
                logger.warning("tile_orientation_unknown_layout", usecase=usecase, size=img.size)
                return data
            ncols, nrows, scale, tile, label_h = grid
            cell_w, cell_h = tile * scale, (tile + label_h) * scale
            out = img.copy()
            for r in range(nrows):
                for c in range(ncols):
                    box = (
                        round(c * cell_w), round(r * cell_h + label_h * scale),
                        round((c + 1) * cell_w), round((r + 1) * cell_h),
                    )
                    out.paste(img.crop(box).transpose(Image.Transpose.FLIP_LEFT_RIGHT), box)
        else:
            out = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # never fail the image request over a display correction
        logger.warning("tile_orientation_correct_failed", usecase=usecase, error=str(exc))
        return data
