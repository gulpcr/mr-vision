from __future__ import annotations

import io

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

_VIEW_AXES: dict[str, int] = {"axial": 2, "coronal": 1, "sagittal": 0}
VIEWS = list(_VIEW_AXES)


def _slice2d(arr: np.ndarray, axis: int, idx: int) -> np.ndarray:
    if axis == 0:
        return arr[idx, :, :]
    elif axis == 1:
        return arr[:, idx, :]
    else:
        return arr[:, :, idx]


def _get_cmap(name: str):
    """Return a matplotlib colormap by name using the stable API (>=3.5)."""
    import matplotlib
    try:
        return matplotlib.colormaps.get_cmap(name)
    except AttributeError:
        import matplotlib.cm as cm
        return cm.get_cmap(name)


def slice_count(suv_arr: np.ndarray, view: str) -> int:
    """Number of slices available for a view (the size along that view's axis)."""
    if view not in _VIEW_AXES:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    return int(suv_arr.shape[_VIEW_AXES[view]])


def best_slice(suv_arr: np.ndarray, view: str) -> int:
    """Slice index with the most PET uptake along the view axis.

    Used as the initial slice for the interactive viewer so it opens on an
    anatomically interesting (high-uptake) plane rather than an empty edge.
    """
    if view not in _VIEW_AXES:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    axis = _VIEW_AXES[view]
    other = tuple(a for a in range(suv_arr.ndim) if a != axis)
    sums = np.clip(suv_arr, 0, None).sum(axis=other)
    if sums.size == 0:
        return int(suv_arr.shape[axis] // 2)
    return int(np.argmax(sums))


def compute_suv_display_range(suv_arr: np.ndarray) -> tuple[float, float]:
    """Per-volume PET display window (vmin, vmax), computed once and cached so
    per-slice rendering doesn't re-scan the whole volume."""
    valid = suv_arr[suv_arr > 0]
    if len(valid) == 0:
        return 0.0, 5.0
    vmax = max(min(float(np.percentile(valid, 99.5)), 10.0), 0.1)
    vmin = float(np.percentile(valid, 5))
    return vmin, vmax


def generate_fused_slice_fast(
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    view: str,
    slice_index: int,
    suv_vmin: float,
    suv_vmax: float,
    colormap: str = "hot",
    alpha: float = 0.65,
    out_size: int = 512,
    mask_arr: np.ndarray | None = None,
    show_lesions: bool = True,
    mode: str = "fused",
) -> bytes:
    """Fast fused-slice PNG using numpy + PIL (no matplotlib figure).

    ~10x faster than :func:`generate_fused_png_bytes`, intended for the
    interactive viewer's per-slice requests. Display window (vmin/vmax) is passed
    in pre-computed. Orientation matches the matplotlib renderer (transpose +
    vertical flip, i.e. origin='lower').

    ``mode`` selects what is rendered:

    * ``"fused"`` (default): CT grayscale background + PET ``colormap`` overlay,
      shown only above 20 % of the display SUV-max (so CT anatomy stays visible).
    * ``"ct"``: CT grayscale only (no PET overlay).
    * ``"pet"``: PET ``colormap`` only, on a black background (no CT) — a plain
      PET render across the full display window.

    When ``mask_arr`` (the detected-lesion segmentation, same grid as ``suv_arr``)
    is supplied and ``show_lesions`` is True, the boundary of each detected lesion
    is outlined in cyan in every mode so the user can distinguish *flagged* foci
    from raw physiologic uptake.
    """
    if view not in _VIEW_AXES:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    if mode not in ("fused", "ct", "pet"):
        raise ValueError(f"mode must be one of fused, ct, pet; got {mode!r}")

    from PIL import Image

    axis = _VIEW_AXES[view]
    n_slices = suv_arr.shape[axis]
    idx = int(max(0, min(slice_index, n_slices - 1)))

    pet_sl = np.flipud(_slice2d(suv_arr, axis, idx).T)
    h, w = pet_sl.shape

    if ct_arr is not None and ct_arr.shape == suv_arr.shape:
        ct_sl = np.flipud(_slice2d(ct_arr, axis, idx).T)
        ct_norm = np.clip((ct_sl + 1000.0) / 2000.0, 0.0, 1.0)
    else:
        ct_norm = np.zeros((h, w), dtype=np.float32)
    rgb = np.repeat((ct_norm * 255.0)[..., None], 3, axis=2)

    denom = max(suv_vmax - suv_vmin, 1e-6)
    pet_norm = np.clip((pet_sl - suv_vmin) / denom, 0.0, 1.0)
    cmap_fn = _get_cmap(colormap)
    pet_rgb = np.asarray(cmap_fn(pet_norm), dtype=np.float64)[..., :3] * 255.0

    if mode == "ct":
        # CT grayscale only.
        out = rgb
    elif mode == "pet":
        # PET colormap only, on black (low SUV is already near-black in 'hot').
        out = pet_rgb
    else:
        # Fused: CT background with PET overlay above the display threshold.
        threshold = suv_vmax * 0.20
        a = np.where(pet_sl >= threshold, alpha, 0.0).astype(np.float64)[..., None]
        out = rgb * (1.0 - a) + pet_rgb * a

    # Outline detected lesions (same orientation transform as the SUV slice).
    if show_lesions and mask_arr is not None and mask_arr.shape == suv_arr.shape:
        mask_sl = np.flipud(_slice2d(mask_arr, axis, idx).T) > 0
        if mask_sl.any():
            from scipy import ndimage

            boundary = mask_sl ^ ndimage.binary_erosion(mask_sl)
            boundary = ndimage.binary_dilation(boundary)  # thicken for visibility
            out[boundary] = np.array([0.0, 255.0, 255.0])  # cyan

    img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")
    if out_size and max(h, w) > 0 and max(h, w) < out_size:
        scale = out_size / max(h, w)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def resample_ct_to_suv(suv_arr: np.ndarray, ct_arr: np.ndarray | None) -> np.ndarray | None:
    """Resample CT onto the SUV grid once so per-slice rendering needs no zoom.

    The interactive viewer caches the result, so the expensive 3-D zoom runs a
    single time per study instead of on every slice request.
    """
    if ct_arr is None or ct_arr.shape == suv_arr.shape:
        return ct_arr
    try:
        from scipy.ndimage import zoom as nd_zoom

        factors = tuple(p / c for p, c in zip(suv_arr.shape, ct_arr.shape))
        return nd_zoom(ct_arr, factors, order=1).astype(np.float32)
    except Exception as exc:
        logger.warning("fused_ct_zoom_failed", error=str(exc))
        return None


def generate_fused_png_bytes(
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    view: str,
    colormap: str = "hot",
    alpha: float = 0.65,
    slice_index: int | None = None,
) -> bytes:
    """Generate a single fused PET/CT PNG and return the raw PNG bytes.

    CT is rendered with the 'bone' colormap as background (or black when absent).
    PET SUV is rendered with `colormap` (default 'hot') as a semi-transparent
    overlay. Only voxels above 20 % of the display SUV-max are shown so that
    CT anatomy remains visible in low-uptake regions.
    """
    if view not in _VIEW_AXES:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize

    axis = _VIEW_AXES[view]
    n_slices = suv_arr.shape[axis]
    if slice_index is None:
        idx = n_slices // 2
    else:
        idx = int(max(0, min(slice_index, n_slices - 1)))

    valid = suv_arr[suv_arr > 0]
    suv_max_disp = min(float(np.percentile(valid, 99.5)), 10.0) if len(valid) > 0 else 5.0
    suv_max_disp = max(suv_max_disp, 0.1)
    suv_min_disp = float(np.percentile(valid, 5)) if len(valid) > 0 else 0.0

    if ct_arr is not None and ct_arr.shape != suv_arr.shape:
        try:
            from scipy.ndimage import zoom as nd_zoom
            factors = tuple(p / c for p, c in zip(suv_arr.shape, ct_arr.shape))
            ct_arr = nd_zoom(ct_arr, factors, order=1).astype(np.float32)
        except Exception as exc:
            logger.warning("fused_ct_zoom_failed", error=str(exc))
            ct_arr = None

    cmap_fn = _get_cmap(colormap)

    fig, ax = plt.subplots(figsize=(5, 5), facecolor="black")
    try:
        if ct_arr is not None:
            ct_sl = _slice2d(ct_arr, axis, idx).T
            ct_norm = np.clip((ct_sl + 1000.0) / 2000.0, 0.0, 1.0)
            ax.imshow(ct_norm, cmap="bone", aspect="auto", origin="lower")

        pet_sl = _slice2d(suv_arr, axis, idx).T
        pet_norm = Normalize(vmin=suv_min_disp, vmax=suv_max_disp)(pet_sl)
        pet_rgba = np.array(cmap_fn(pet_norm), dtype=np.float64)
        pet_threshold = suv_max_disp * 0.20
        pet_rgba[..., 3] = np.where(pet_sl >= pet_threshold, alpha, 0.0)
        ax.imshow(pet_rgba, aspect="auto", origin="lower")

        ax.axis("off")
        title = f"{view.capitalize()} — Fused PET/CT"
        if slice_index is not None:
            title += f"  [{idx + 1}/{n_slices}]"
        ax.set_title(title, color="white", fontsize=9, pad=4)

        buf = io.BytesIO()
        fig.savefig(buf, dpi=120, bbox_inches="tight", facecolor="black", format="png")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)
