"""Generate preview PNG images with segmentation overlay on MRI slices.

Produces axial, coronal, and sagittal views at the slice with maximum
tumour content, with coloured segmentation labels blended on top.

Both the background and segmentation volumes are reoriented to canonical
RAS before slicing so the axes are always:
  axis 0 → sagittal   (R–L)
  axis 1 → coronal    (A–P)
  axis 2 → axial      (S–I)
regardless of how the NIfTI was originally saved (e.g. SimpleITK ZYX).
"""

from __future__ import annotations

import io
import os
from typing import Any

import nibabel as nib
import numpy as np
from PIL import Image

# Default BraTS colormap: label_id -> (R, G, B)
DEFAULT_COLORMAP: dict[int, tuple[int, int, int]] = {
    1: (255, 107, 107),   # Tumor Core  — red
    2: (78, 205, 196),    # Whole Tumor — teal
    3: (255, 204, 2),     # Enhancing   — yellow
}

# After canonical RAS reorientation: axis 0=sagittal, 1=coronal, 2=axial
VIEWS = [
    ("axial",    2),
    ("coronal",  1),
    ("sagittal", 0),
]

OVERLAY_ALPHA = 0.55


def _to_ras(img: nib.Nifti1Image) -> nib.Nifti1Image:
    """Reorient a NIfTI image to the closest canonical (RAS) orientation."""
    return nib.as_closest_canonical(img)


def _render_slice(
    bg_slice: np.ndarray,
    seg_slice: np.ndarray,
    cmap: dict[int, tuple[int, int, int]],
    view: str,
) -> np.ndarray:
    """Render a single 2-D slice as an RGB numpy array (uint8, H×W×3)."""
    # Normalise background to 0-255 using robust percentile windowing
    fg = bg_slice > 0
    p2, p98 = (np.percentile(bg_slice[fg], [2, 98]) if fg.any() else (0.0, 1.0))
    bg_norm = np.clip(
        (bg_slice.astype(np.float32) - p2) / max(float(p98 - p2), 1e-6) * 255,
        0, 255,
    ).astype(np.uint8)

    # Grayscale background — works correctly for both MRI and CT
    rgb = np.stack([bg_norm, bg_norm, bg_norm], axis=-1).copy()

    # Blend segmentation overlay labels
    for label_id, color in cmap.items():
        mask = seg_slice == label_id
        if not mask.any():
            continue
        for c in range(3):
            rgb[mask, c] = np.clip(
                (1 - OVERLAY_ALPHA) * rgb[mask, c] + OVERLAY_ALPHA * color[c],
                0, 255,
            ).astype(np.uint8)

        # Thin bright border for contrast
        from scipy.ndimage import binary_dilation
        border = binary_dilation(mask, iterations=1) & ~mask
        for c in range(3):
            rgb[border, c] = np.clip(color[c] * 1.2, 0, 255).astype(np.uint8)

    # Rotate to standard radiological display orientation
    # After RAS reorientation:
    #   axial   slice shape (R–L, A–P)  → rot90 once → anterior up, R on left
    #   coronal slice shape (R–L, S–I)  → rot90 once → superior up, R on left
    #   sagittal slice shape (A–P, S–I) → rot90 once → superior up, anterior right
    k_map = {"axial": 1, "coronal": 1, "sagittal": 1}
    rgb = np.rot90(rgb, k=k_map.get(view, 1))

    return rgb


def generate_preview_pngs(
    background_nifti_path: str,
    segmentation_nifti_path: str,
    output_dir: str,
    colormap: dict[int, tuple[int, int, int]] | None = None,
    target_size: int = 512,
) -> list[dict[str, Any]]:
    """Render overlay previews and save as PNG files.

    Parameters
    ----------
    background_nifti_path : str
        Path to the MRI/CT volume NIfTI (can be multichannel; channel 0 is used).
    segmentation_nifti_path : str
        Path to the segmentation label NIfTI (uint8, labels 0-N).
    output_dir : str
        Directory to write PNG files into.
    colormap : dict, optional
        Mapping of label int to RGB tuple.
    target_size : int
        Output image will be resized so the longest edge equals this.

    Returns
    -------
    list[dict]
        Artifact metadata dicts for each generated PNG.
    """
    cmap = colormap or DEFAULT_COLORMAP
    os.makedirs(output_dir, exist_ok=True)

    # Load and reorient both volumes to canonical RAS
    bg_img  = _to_ras(nib.load(background_nifti_path))
    seg_img = _to_ras(nib.load(segmentation_nifti_path))

    bg_data  = np.asarray(bg_img.dataobj,  dtype=np.float32)
    seg_data = np.asarray(seg_img.dataobj, dtype=np.uint8)

    # Multichannel background — take first channel (T1 for brain_mri)
    if bg_data.ndim == 4:
        bg_data = bg_data[..., 0] if bg_data.shape[-1] <= 4 else bg_data[0]

    # Crop to common shape if minor mismatch (e.g. from resampling)
    if bg_data.shape != seg_data.shape:
        min_shape = tuple(min(b, s) for b, s in zip(bg_data.shape, seg_data.shape))
        bg_data  = bg_data[ :min_shape[0], :min_shape[1], :min_shape[2]]
        seg_data = seg_data[:min_shape[0], :min_shape[1], :min_shape[2]]

    artifacts = []

    for view_name, axis in VIEWS:
        # Pick the slice with the most tumour voxels; fall back to middle slice
        other_axes  = tuple(i for i in range(3) if i != axis)
        tumor_mask  = seg_data > 0
        voxel_counts = tumor_mask.sum(axis=other_axes)
        best_idx    = int(np.argmax(voxel_counts))
        if voxel_counts[best_idx] == 0:
            best_idx = seg_data.shape[axis] // 2

        slicers = [slice(None)] * 3
        slicers[axis] = best_idx

        bg_slice  = bg_data[ tuple(slicers)]
        seg_slice = seg_data[tuple(slicers)]

        rgb = _render_slice(bg_slice, seg_slice, cmap, view_name)

        pil_img = Image.fromarray(rgb)
        w, h = pil_img.size
        scale = target_size / max(w, h)
        pil_img = pil_img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )

        filename = f"preview_{view_name}.png"
        filepath = os.path.join(output_dir, filename)
        pil_img.save(filepath, "PNG", optimize=True)

        artifacts.append({
            "name": filename,
            "artifact_type": "preview_image",
            "local_path": filepath,
            "content_type": "image/png",
        })

    return artifacts


def generate_preview_bytes(
    background_data: np.ndarray,
    segmentation_data: np.ndarray,
    view: str = "axial",
    colormap: dict[int, tuple[int, int, int]] | None = None,
    target_size: int = 512,
) -> bytes:
    """Generate a single preview PNG from numpy arrays already in RAS order.

    Used by the on-demand preview endpoint which resamples the background
    to the segmentation grid (both already in the same space).
    """
    cmap = colormap or DEFAULT_COLORMAP

    axis_map = {"axial": 2, "coronal": 1, "sagittal": 0}
    axis = axis_map.get(view, 2)

    bg  = background_data.copy().astype(np.float32)
    seg = segmentation_data.copy()

    if bg.ndim == 4:
        bg = bg[..., 0] if bg.shape[-1] <= 4 else bg[0]

    if bg.shape != seg.shape:
        min_shape = tuple(min(b, s) for b, s in zip(bg.shape, seg.shape))
        bg  = bg[ :min_shape[0], :min_shape[1], :min_shape[2]]
        seg = seg[:min_shape[0], :min_shape[1], :min_shape[2]]

    other_axes   = tuple(i for i in range(3) if i != axis)
    tumor_counts = (seg > 0).sum(axis=other_axes)
    best_idx     = int(np.argmax(tumor_counts))
    if tumor_counts[best_idx] == 0:
        best_idx = seg.shape[axis] // 2

    slicers       = [slice(None)] * 3
    slicers[axis] = best_idx

    rgb = _render_slice(bg[tuple(slicers)], seg[tuple(slicers)], cmap, view)

    pil_img = Image.fromarray(rgb)
    w, h    = pil_img.size
    scale   = target_size / max(w, h)
    pil_img = pil_img.resize(
        (max(1, int(w * scale)), max(1, int(h * scale))),
        Image.Resampling.LANCZOS,
    )

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
