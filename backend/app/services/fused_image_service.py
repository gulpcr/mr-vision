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


def generate_fused_png_bytes(
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    view: str,
    colormap: str = "hot",
    alpha: float = 0.65,
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
    x, y, z = suv_arr.shape
    idx = (x // 2, y // 2, z // 2)[axis]

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
        ax.set_title(f"{view.capitalize()} — Fused PET/CT", color="white", fontsize=9, pad=4)

        buf = io.BytesIO()
        fig.savefig(buf, dpi=120, bbox_inches="tight", facecolor="black", format="png")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)
