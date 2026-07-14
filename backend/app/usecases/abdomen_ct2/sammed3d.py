from __future__ import annotations

"""SAM-Med3D promptable segmentation → tumour dimensions (mm).

Accurate size needs a real 3-D boundary, which thresholding can't give for an
indistinct-margin mass. SAM-Med3D (a 3-D promptable SAM) segments the object around
point prompts. We drive it from ``measurement.mass_localization``:

  * ROI is a 128³ crop centred on the mass seed;
  * MULTIPLE positive clicks are sampled across the mass "core" (farthest-point
    sampling of the soft-tissue, non-organ residual) so a large heterogeneous mass is
    anchored across its extent, not just at one point;
  * the predicted mask is TRIMMED by the TotalSegmentator organ mask (removes leakage
    into bladder / bowel), keeps its largest component, and fills holes;
  * dimensions = mask bounding box × the DICOM voxel spacing → TS × AP × CC.

The vendored repo is ``backend/external/SAM-Med3D-main`` (checkpoint in ``.../ckpt``).
Fully non-blocking — returns None on any failure so the report omits a size. Runs in
the worker (torch + torchio + the checkpoint).
"""

import os
import sys
from typing import Any

import nibabel as nib
import numpy as np
import structlog

logger = structlog.get_logger(__name__)

_REPO = os.environ.get("SAMMED3D_REPO", "/app/external/SAM-Med3D-main")
_CKPT = os.environ.get("SAMMED3D_CKPT", os.path.join(_REPO, "ckpt", "sam_med3d_turbo.pth"))

_MODEL: Any = None  # lazy singleton (kept resident per worker)


def _load_model() -> Any:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    import torch

    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    from segment_anything import build_sam3D_vit_b_ori  # type: ignore

    if not os.path.exists(_CKPT):
        logger.warning("sammed3d_no_checkpoint", ckpt=_CKPT)
        return None
    sam = build_sam3D_vit_b_ori(checkpoint=None)  # sam_med3d_turbo == vit_b_ori (embed 768)
    state = torch.load(_CKPT, map_location="cpu")
    if isinstance(state, dict):
        state = state.get("model_state_dict", state.get("state_dict", state))
    sam.load_state_dict(state, strict=False)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _MODEL = sam.to(device).eval()
    logger.info("sammed3d_loaded", device=device, ckpt=os.path.basename(_CKPT))
    return _MODEL


def _fps(points: np.ndarray, k: int) -> np.ndarray:
    """Farthest-point sampling of ``k`` spread points, seeded from the centroid."""
    if len(points) <= k:
        return points
    centroid = points.mean(0)
    sel = [int(np.linalg.norm(points - centroid, axis=1).argmin())]
    dist = np.full(len(points), np.inf)
    for _ in range(k - 1):
        dist = np.minimum(dist, np.linalg.norm(points - points[sel[-1]], axis=1))
        sel.append(int(dist.argmax()))
    return points[sel]


def _preprocess(volume_path, seed_path, core_path, organ_path, target_spacing, crop=128):
    """Resample→canonical→128³ crop centred on the seed → z-normalize (public torchio).

    Returns ``(roi_image (1,1,D,H,W), core_roi (D,H,W bool | None), organ_roi (bool | None), meta)``.
    """
    import torchio as tio

    img = nib.load(volume_path)
    meta: dict[str, Any] = {"sitk_spacing": tuple(float(z) for z in img.header.get_zooms()[:3])}

    parts: dict[str, Any] = {"image": tio.ScalarImage(volume_path), "seed": tio.LabelMap(seed_path)}
    if core_path and os.path.exists(core_path):
        parts["core"] = tio.LabelMap(core_path)
    if organ_path and os.path.exists(organ_path):
        parts["organ"] = tio.LabelMap(organ_path)
    subject = tio.Subject(**parts)
    meta["original_subject_affine"] = subject.image.affine.copy()
    meta["original_subject_spatial_shape"] = subject.image.spatial_shape

    subject = tio.Resample((target_spacing, target_spacing, target_spacing))(subject)
    subject = tio.ToCanonical()(subject)
    subject = tio.CropOrPad(target_shape=(crop, crop, crop), mask_name="seed")(subject)
    meta["roi_subject_affine"] = subject.image.affine.copy()
    subject = tio.ZNormalization(masking_method=lambda x: x > 0)(subject)

    roi = subject.image.data.float().unsqueeze(0)  # (1,1,D,H,W)
    core_roi = (subject.core.data[0].numpy() > 0) if "core" in parts else None
    organ_roi = (subject.organ.data[0].numpy() > 0) if "organ" in parts else None
    return roi, core_roi, organ_roi, meta


def _infer(model, roi_image, coords, labels, thr: float) -> np.ndarray:
    """Run SAM-Med3D with explicit click ``coords`` (list of X,Y,Z) → binary ROI mask."""
    import torch
    import torch.nn.functional as F

    device = next(model.parameters()).device
    with torch.no_grad():
        x = roi_image.to(device)
        img_emb = model.image_encoder(x)
        d, h, w = x.shape[-3:]
        pc = torch.tensor([coords], dtype=torch.float, device=device)   # (1, N, 3)
        pl = torch.tensor([labels], dtype=torch.int64, device=device)   # (1, N)
        prev = torch.zeros(1, 1, d // 4, h // 4, w // 4, device=device, dtype=torch.float)
        sparse, dense = model.prompt_encoder(points=[pc, pl], boxes=None, masks=prev)
        low_res, _ = model.mask_decoder(
            image_embeddings=img_emb,
            image_pe=model.prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse,
            dense_prompt_embeddings=dense,
            multimask_output=False,
        )
        hr = F.interpolate(low_res, size=x.shape[-3:], mode="trilinear", align_corners=False)
        prob = torch.sigmoid(hr).cpu().numpy().squeeze()
    return (prob > thr).astype(np.uint8)


def segment_and_measure(
    volume_path: str,
    localization: dict[str, Any],
    working_dir: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Segment the mass with SAM-Med3D and return TS/AP/CC mm, or None."""
    cfg = cfg or {}
    try:
        model = _load_model()
    except Exception as exc:
        logger.warning("sammed3d_load_failed", error=str(exc))
        return None
    if model is None:
        return None
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    try:
        from utils.infer_utils import data_postprocess  # type: ignore
    except Exception as exc:
        logger.warning("sammed3d_import_failed", error=str(exc))
        return None

    seed = tuple(int(v) for v in localization["seed_xyz"])
    core_path = localization.get("core_path")
    organ_path = localization.get("organ_path")

    # seed blob (label) sharing the volume grid → centres the ROI crop.
    img = nib.load(volume_path)
    shape = tuple(int(s) for s in img.shape[:3])
    x, y, z = seed
    r = int(cfg.get("seed_radius", 3))
    blob = np.zeros(shape, dtype=np.uint8)
    blob[max(0, x - r):x + r + 1, max(0, y - r):y + r + 1, max(0, z - r):z + r + 1] = 1
    if blob.sum() == 0:
        return None
    seed_path = os.path.join(working_dir, "sammed3d_seed.nii.gz")
    nib.save(nib.Nifti1Image(blob, img.affine, img.header), seed_path)

    ts = float(cfg.get("target_spacing", 1.0))
    thr = float(cfg.get("mask_thresh", 0.45))
    n_clicks = int(cfg.get("n_clicks", 12))
    try:
        roi_image, core_roi, organ_roi, meta = _preprocess(volume_path, seed_path, core_path, organ_path, ts)
        d, h, w = roi_image.shape[-3:]
        # positive clicks: farthest-point set across the mass core (else ROI centre).
        coords: list[list[int]] = [[w // 2, h // 2, d // 2]]
        labels: list[int] = [1]
        if core_roi is not None and core_roi.any():
            pts = np.argwhere(core_roi)  # (N, 3) in (D, H, W)
            for di, hi, wi in _fps(pts, max(1, n_clicks)):
                coords.append([int(wi), int(hi), int(di)])  # SAM order X,Y,Z
                labels.append(1)
        mask = _infer(model, roi_image, coords, labels, thr)          # (D,H,W) ROI
        if organ_roi is not None:                                     # trim labeled-organ leakage
            mask = (mask.astype(bool) & ~organ_roi).astype(np.uint8)
        mask = data_postprocess(mask, meta)                           # → original grid (Z,Y,X)
    except Exception as exc:
        logger.warning("sammed3d_infer_failed", error=str(exc))
        return None

    # cleanup: largest connected component + fill holes.
    from scipy import ndimage

    mask = np.asarray(mask) > 0
    lbl, n = ndimage.label(mask)
    if n == 0:
        logger.info("sammed3d_empty_mask")
        return None
    counts = np.bincount(lbl.ravel())
    counts[0] = 0
    mask = ndimage.binary_fill_holes(lbl == int(counts.argmax()))

    # Trim thin cranio-caudal leaks (into unlabeled uterus/bowel): keep the contiguous
    # z-block around the peak cross-section where slice area >= frac × peak. mask is Z,Y,X.
    areas = mask.sum(axis=(1, 2))
    if areas.max() > 0:
        pk = int(areas.argmax())
        floor = float(cfg.get("z_area_frac", 0.3)) * float(areas.max())
        lo = hi = pk
        while lo - 1 >= 0 and areas[lo - 1] >= floor:
            lo -= 1
        while hi + 1 < len(areas) and areas[hi + 1] >= floor:
            hi += 1
        trimmed = np.zeros_like(mask)
        trimmed[lo:hi + 1] = mask[lo:hi + 1]
        mask = trimmed

    n_vox = int(mask.sum())
    if n_vox < int(cfg.get("min_voxels", 50)):
        return None

    sx, sy, sz = (float(s) for s in meta["sitk_spacing"])
    zz, yy, xx = np.where(mask)
    ts_mm = (xx.max() - xx.min() + 1) * sx
    ap_mm = (yy.max() - yy.min() + 1) * sy
    cc_mm = (zz.max() - zz.min() + 1) * sz
    result = {
        "ts_mm": round(float(ts_mm), 1),
        "ap_mm": round(float(ap_mm), 1),
        "cc_mm": round(float(cc_mm), 1),
        "volume_ml": round(n_vox * sx * sy * sz / 1000.0, 1),
        "n_voxels": n_vox,
        "n_clicks": len(coords),
        "seed_xyz": [x, y, z],
        "spacing_mm": [round(sx, 2), round(sy, 2), round(sz, 2)],
        "method": "sam_med3d_turbo",
        "estimate": True,
    }
    logger.info("sammed3d_measure_done", **{k: result[k] for k in ("ts_mm", "ap_mm", "cc_mm", "volume_ml", "n_clicks")})
    return result
