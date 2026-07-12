"""Stage 3 — Composite image fusion & label burning.

Two families of renderers:

1. **Existing per-view renderers** (``generate_mip_pngs``,
   ``generate_fused_petct_pngs``) — moved verbatim from the monolith so the
   study viewer and PDF report keep the exact artifacts they already consume.

2. **MedGemma-oriented composites** (the new methodology) — designed to give a
   vision-language model a compact, information-dense read instead of 15-20
   disorienting single-lesion crops:

   * ``generate_lesion_mip_roadmap`` — ONE full-body coronal MIP
     (``np.max(pet, axis=1)``) with numbered dots marking every lesion. This is
     the macro roadmap that anchors the model spatially.
   * ``generate_composite_crops`` — nearby lesions (liver cluster, pelvic
     cluster, …) are drawn on a SINGLE regional crop rather than separate images.
     Each crop alpha-blends the windowed CT (soft-tissue, HU −100…200) with the
     colorized PET (background below SUV 1.5 fully transparent), outlines lesions
     with cyan ``contour`` lines, and burns high-contrast ``#1/#2/#3`` labels so
     the model can reference each lesion unambiguously.

Array-axis convention: volumes are ``(X, Y, Z)``; coronal projection is along
axis 1, axial slices are ``[:, :, z]`` (see ``registration`` module docstring).
Display transforms (``.T`` + ``origin='lower'``) match ``fused_image_service`` so
overlays register with the interactive viewer.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

# SUV below this is physiologic background — made fully transparent in overlays.
_PET_TRANSPARENT_BELOW_SUV = 1.5
# CT soft-tissue window for the composite background.
_CT_WINDOW = (-100.0, 200.0)


# ── Existing per-view renderers (moved verbatim) ────────────────────────────────

def generate_mip_pngs(suv_arr: np.ndarray, output_dir: str, colormap: str = "hot") -> list[dict]:
    """Generate axial, coronal, sagittal Maximum Intensity Projection PNGs."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import Normalize
        import matplotlib.cm as cm
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_mip")
        return []

    os.makedirs(output_dir, exist_ok=True)
    valid = suv_arr[suv_arr > 0]
    disp_max = min(float(np.percentile(valid, 99.5)), 10.0) if len(valid) > 0 else 1.0
    disp_max = max(disp_max, 0.1)

    artifacts = []
    view_axes = {"axial": 2, "coronal": 1, "sagittal": 0}

    for view_name, axis in view_axes.items():
        try:
            mip = np.max(suv_arr, axis=axis)
            fig, ax = plt.subplots(figsize=(5, 9), facecolor="black")
            ax.imshow(mip.T, cmap=colormap, vmin=0, vmax=disp_max, aspect="auto", origin="lower")
            ax.axis("off")
            ax.set_title(f"{view_name.capitalize()} MIP", color="white", fontsize=9, pad=4)
            cbar = fig.colorbar(
                cm.ScalarMappable(norm=Normalize(0, disp_max), cmap=colormap),
                ax=ax, fraction=0.03, pad=0.02,
            )
            cbar.set_label("SUV", color="white", fontsize=8)
            cbar.ax.yaxis.set_tick_params(color="white", labelcolor="white")

            png_path = os.path.join(output_dir, f"mip_{view_name}.png")
            fig.savefig(png_path, dpi=120, bbox_inches="tight", facecolor="black")
            plt.close(fig)

            artifacts.append({
                "name": f"mip_{view_name}.png",
                "artifact_type": "mip_png",
                "local_path": png_path,
                "content_type": "image/png",
            })
        except Exception as e:
            logger.error("mip_png_view_failed", view=view_name, error=str(e))
            try:
                plt.close("all")
            except Exception:
                pass

    return artifacts


def generate_fused_petct_pngs(
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    output_dir: str,
    colormap: str = "hot",
    alpha: float = 0.65,
) -> list[dict]:
    """Generate fused PET-on-CT overlay PNGs for axial, coronal, sagittal views."""
    try:
        from app.services.fused_image_service import generate_fused_png_bytes, VIEWS
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_fused")
        return []

    os.makedirs(output_dir, exist_ok=True)
    artifacts = []
    for view_name in VIEWS:
        try:
            png_bytes = generate_fused_png_bytes(suv_arr, ct_arr, view_name, colormap, alpha)
            png_path = os.path.join(output_dir, f"fused_{view_name}.png")
            with open(png_path, "wb") as fh:
                fh.write(png_bytes)
            artifacts.append({
                "name": f"fused_{view_name}.png",
                "artifact_type": "fused_png",
                "local_path": png_path,
                "content_type": "image/png",
            })
        except Exception as exc:
            logger.error("fused_png_view_failed", view=view_name, error=str(exc))

    return artifacts


# ── MedGemma-oriented composites (Stage 3, new) ─────────────────────────────────

def _disp_suv_max(suv_arr: np.ndarray) -> float:
    valid = suv_arr[suv_arr > 0]
    if len(valid) == 0:
        return 5.0
    return max(min(float(np.percentile(valid, 99.5)), 10.0), 0.1)


def generate_lesion_mip_roadmap(
    suv_arr: np.ndarray,
    lesions: list[dict[str, Any]],
    output_dir: str,
    colormap: str = "hot",
) -> dict | None:
    """Single coronal-MIP roadmap of the whole body with numbered lesion markers.

    ``np.max(suv_arr, axis=1)`` collapses the anterior-posterior axis into a
    coronal maximum-intensity projection. Each lesion's epicenter (``x``, ``z``)
    is plotted as a numbered dot, giving MedGemma one macro image that ties every
    ``#n`` label to a body location before it reads the regional crops.

    Returns a single artifact dict, or None if rendering is unavailable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_roadmap")
        return None

    os.makedirs(output_dir, exist_ok=True)
    disp_max = _disp_suv_max(suv_arr)

    try:
        mip = np.max(suv_arr, axis=1)  # (X, Z)
        fig, ax = plt.subplots(figsize=(5, 9), facecolor="black")
        # mip.T → (Z, X); origin='lower' puts the head (high Z) at the top.
        ax.imshow(mip.T, cmap=colormap, vmin=0, vmax=disp_max, aspect="auto", origin="lower")
        ax.axis("off")
        ax.set_title("Coronal MIP — Lesion Roadmap", color="white", fontsize=10, pad=6)

        for les in lesions:
            epi = les.get("epicenter_voxel") or les.get("centroid_voxel")
            if not epi:
                continue
            x_vox, _, z_vox = epi
            # Data coords after .T + origin='lower': x-axis = X index, y-axis = Z index.
            ax.plot(x_vox, z_vox, "o", markersize=9, markerfacecolor="none",
                    markeredgecolor="cyan", markeredgewidth=1.6)
            ax.text(
                x_vox + 4, z_vox + 4, f"#{les['id']}",
                color="white", fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.15", facecolor="black", edgecolor="cyan", alpha=0.8),
            )

        png_path = os.path.join(output_dir, "lesion_roadmap.png")
        fig.savefig(png_path, dpi=130, bbox_inches="tight", facecolor="black")
        plt.close(fig)
        return {
            "name": "lesion_roadmap.png",
            "artifact_type": "lesion_roadmap_png",
            "local_path": png_path,
            "content_type": "image/png",
        }
    except Exception as exc:
        logger.error("lesion_roadmap_failed", error=str(exc))
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        return None


def cluster_lesions(
    lesions: list[dict[str, Any]],
    voxel_spacing_mm: tuple[float, float, float],
    cluster_distance_mm: float = 60.0,
) -> list[list[int]]:
    """Group lesions whose epicenters are within ``cluster_distance_mm`` (single-link).

    Nearby lesions (e.g. multiple hepatic foci, a pelvic nodal cluster) share one
    composite crop instead of generating separate near-identical images — the key
    to keeping the MedGemma image count (and token cost) bounded. Returns a list
    of index-groups into ``lesions``.
    """
    n = len(lesions)
    if n == 0:
        return []
    spacing = np.asarray(voxel_spacing_mm, dtype=float)
    epis = []
    for les in lesions:
        epi = les.get("epicenter_voxel") or les.get("centroid_voxel") or [0, 0, 0]
        epis.append(np.asarray(epi, dtype=float) * spacing)  # → mm
    epis = np.asarray(epis)

    # Union-find over the pairwise mm distance graph.
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if np.linalg.norm(epis[i] - epis[j]) <= cluster_distance_mm:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def generate_composite_crops(
    suv_arr: np.ndarray,
    ct_arr: np.ndarray | None,
    lesion_labels: np.ndarray,
    lesions: list[dict[str, Any]],
    output_dir: str,
    voxel_spacing_mm: tuple[float, float, float],
    colormap: str = "hot",
    cluster_distance_mm: float = 60.0,
    crop_margin_vox: int = 24,
    max_crops: int = 5,
) -> list[dict]:
    """One alpha-blended, labeled axial crop per lesion cluster.

    For each cluster the representative axial slice is the mean epicenter z. On
    that slice we:

    * render the windowed CT (grayscale, HU −100…200) as anatomical background;
    * overlay the colorized PET with SUV < 1.5 fully transparent and alpha 0.5;
    * draw cyan ``contour`` outlines of each member lesion's mask;
    * burn a high-contrast ``#n`` label offset from each lesion boundary.

    ``max_crops`` bounds the image count for token safety; if clusters exceed it,
    the excess is logged (never silently dropped). Returns artifact dicts.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib_not_available_skipping_composite")
        return []

    if not lesions:
        return []

    os.makedirs(output_dir, exist_ok=True)
    disp_max = _disp_suv_max(suv_arr)
    x_dim, y_dim, z_dim = suv_arr.shape

    # ── BUG-1 fix: guarantee the lesion mask shares the PET/SUV voxel grid ─────
    # ax.contour() draws in the array's index space, and ax.imshow() draws the
    # PET/CT in that SAME index space ONLY when the arrays have identical shape.
    # If lesion_labels is on a different grid (CT-space or a down/upsampled Stage-1
    # grid) the per-slice mask has different dimensions, so contour and imshow no
    # longer share a coordinate frame: matplotlib rescales the contour into the
    # axes and the cyan lands on translated (wrong) tissue while keeping the
    # lesion's shape — the "shifted but same geometry, over cold tissue" symptom.
    # The CT is already shape-checked above; the mask was not. Resample it onto the
    # SUV grid with NEAREST-neighbor (a segmentation is categorical — linear interp
    # would invent fractional edge labels). NOTE: a shape-only zoom is correct only
    # when mask and SUV share a field of view; if they have different affines /
    # origins, the mask must be affine-resampled onto the PET grid UPSTREAM via
    # registration.resample_labelmap_to_reference — a zoom cannot undo an origin
    # offset. In this pipeline the mask is already on the SUV grid, so this is a
    # defensive no-op that hardens the renderer against a decoupled mask.
    if lesion_labels.shape != suv_arr.shape:
        from scipy.ndimage import zoom

        logger.warning(
            "composite_mask_grid_mismatch_resampling",
            mask_shape=tuple(lesion_labels.shape), suv_shape=tuple(suv_arr.shape),
        )
        factors = tuple(s / m for s, m in zip(suv_arr.shape, lesion_labels.shape))
        lesion_labels = zoom((lesion_labels > 0).astype(np.float32), factors, order=0) > 0.5

    groups = cluster_lesions(lesions, voxel_spacing_mm, cluster_distance_mm)
    if len(groups) > max_crops:
        logger.warning(
            "composite_crops_capped",
            total_clusters=len(groups), rendered=max_crops,
            dropped=len(groups) - max_crops,
        )
        groups = groups[:max_crops]

    artifacts: list[dict] = []
    for gi, group in enumerate(groups, start=1):
        try:
            members = [lesions[i] for i in group]
            epis = [m.get("epicenter_voxel") or m.get("centroid_voxel") or [0, 0, 0] for m in members]
            z = int(round(float(np.mean([e[2] for e in epis]))))
            z = max(0, min(z, z_dim - 1))

            # Axial slices (X, Y) at z; display as .T + origin lower (matches viewer).
            pet_sl = suv_arr[:, :, z].T
            ct_sl = ct_arr[:, :, z].T if (ct_arr is not None and ct_arr.shape == suv_arr.shape) else None
            mask_sl = (lesion_labels[:, :, z] > 0).T

            # Crop to the union bounding box of member epicenters + margin.
            xs = [e[0] for e in epis]
            ys = [e[1] for e in epis]
            x0 = max(0, min(xs) - crop_margin_vox)
            x1 = min(x_dim, max(xs) + crop_margin_vox + 1)
            y0 = max(0, min(ys) - crop_margin_vox)
            y1 = min(y_dim, max(ys) + crop_margin_vox + 1)

            fig, ax = plt.subplots(figsize=(5, 5), facecolor="black")

            # CT grayscale background, soft-tissue window.
            if ct_sl is not None:
                ax.imshow(
                    ct_sl, cmap="gray", vmin=_CT_WINDOW[0], vmax=_CT_WINDOW[1],
                    aspect="equal", origin="lower",
                )

            # PET overlay: SUV < 1.5 fully transparent, alpha 0.5 elsewhere.
            # NB: matplotlib.cm.get_cmap was removed in 3.9 — use the registry
            # accessor (project pins matplotlib>=3.8, runs 3.10).
            from matplotlib.colors import Normalize
            cmap_fn = matplotlib.colormaps[colormap]
            pet_norm = Normalize(vmin=0.0, vmax=disp_max)(pet_sl)
            pet_rgba = np.asarray(cmap_fn(pet_norm), dtype=float)
            pet_rgba[..., 3] = np.where(pet_sl >= _PET_TRANSPARENT_BELOW_SUV, 0.5, 0.0)
            ax.imshow(pet_rgba, aspect="equal", origin="lower")

            # Cyan lesion contours.
            if mask_sl.any():
                ax.contour(mask_sl.astype(float), levels=[0.5], colors="cyan", linewidths=1.4)

            # Burned-in numbered labels, offset from each epicenter.
            for m, epi in zip(members, epis):
                ax.text(
                    epi[0] + 3, epi[1] + 3, f"#{m['id']}",
                    color="white", fontsize=11, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", facecolor="black",
                              edgecolor="cyan", alpha=0.85),
                )

            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
            ax.axis("off")
            region = members[0].get("structure") or members[0].get("anatomical_region") or "region"
            ax.set_title(
                f"Composite #{gi} — {region} (z={z}, {len(members)} focus/foci)",
                color="white", fontsize=9, pad=4,
            )

            png_path = os.path.join(output_dir, f"composite_region_{gi}.png")
            fig.savefig(png_path, dpi=130, bbox_inches="tight", facecolor="black")
            plt.close(fig)
            artifacts.append({
                "name": f"composite_region_{gi}.png",
                "artifact_type": "composite_crop_png",
                "local_path": png_path,
                "content_type": "image/png",
            })
        except Exception as exc:
            logger.error("composite_crop_failed", cluster=gi, error=str(exc))
            try:
                import matplotlib.pyplot as plt
                plt.close("all")
            except Exception:
                pass

    return artifacts
