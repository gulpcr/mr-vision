from __future__ import annotations

"""Prototype tumour-measurement for abdomen_ct.

The report can only state a size in mm if we have a real 3-D extent — a VLM cannot
measure. This module derives it geometrically: it isolates the dominant soft-tissue
MASS on the flagged levels and measures its bounding box against the DICOM voxel
spacing (already encoded in the NIfTI affine). Dimensions in mm = voxel extent ×
spacing, reported TS × AP × CC.

Localization method (v1, no extra model — TotalSegmentator is already in the worker):
  1. Restrict to the axial z-range that was flagged as a mass.
  2. Keep SOLID soft-tissue voxels (HU in [hu_min, hu_max]) — excludes ascites/fat
     (too low) and bone/dense contrast (too high).
  3. Subtract every NORMAL organ TotalSegmentator labels (bladder, bowel, muscle,
     bone, vessels…) so only the *unlabeled* abnormal soft tissue (the mass) remains.
  4. Morphological opening to break thin bridges, then take the largest 3-D connected
     component as the mass.
  5. Measure its bounding box × spacing.

This is an ESTIMATE (segmentation of an indistinct-margin mass is imperfect) — the
caller must render it as "AI-estimated, radiologist to confirm". Non-blocking: returns
None on any failure so the report simply omits a size. Runs in the Celery worker
(needs TotalSegmentator + GPU); does NOT touch the scan/flag flow.
"""

import os
import re
from typing import Any

import nibabel as nib
import numpy as np
import structlog

logger = structlog.get_logger(__name__)

_MASS_RE = re.compile(r"mass|tumou?r|neoplas|adnexal|lesion|carcinoma", re.IGNORECASE)


def measure_dominant_mass(
    volume_path: str,
    flagged: list[dict[str, Any]],
    working_dir: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Measure the dominant flagged mass → ``{ts_mm, ap_mm, cc_mm, volume_ml, ...}`` or None."""
    cfg = cfg or {}
    hu_min = float(cfg.get("hu_min", 20.0))
    hu_max = float(cfg.get("hu_max", 200.0))
    z_pad = int(cfg.get("z_pad", 2))
    min_voxels = int(cfg.get("min_voxels", 50))

    try:
        from scipy import ndimage
    except Exception:
        logger.warning("abdomen_ct_measure_no_scipy")
        return None

    if not os.path.exists(volume_path):
        logger.warning("abdomen_ct_measure_no_volume", path=volume_path)
        return None

    img = nib.load(volume_path)
    arr = np.squeeze(np.asarray(img.get_fdata(), dtype=np.float32))
    while arr.ndim > 3:
        arr = arr[..., 0]
    if arr.ndim != 3:
        return None
    sx, sy, sz = (float(z) for z in img.header.get_zooms()[:3])
    X, Y, Z = arr.shape

    # 1. z-range flagged as a mass (fall back to all flagged levels).
    mass_z = sorted({int(f["z"]) for f in flagged if f.get("finding") and _MASS_RE.search(str(f["finding"]))})
    if not mass_z:
        mass_z = sorted({int(f["z"]) for f in flagged if "z" in f})
    if not mass_z:
        return None
    # Use the DENSEST contiguous cluster of flagged levels — a lone far-off flag (a
    # mislabel) must not blow the z-range across the whole abdomen.
    gap = int(cfg.get("cluster_gap", 25))
    clusters: list[list[int]] = [[mass_z[0]]]
    for z in mass_z[1:]:
        (clusters[-1].append(z) if z - clusters[-1][-1] <= gap else clusters.append([z]))
    best = max(clusters, key=len)
    z0 = max(0, min(best) - z_pad)          # tight flagged range → the SEED region
    z1 = min(Z - 1, max(best) + z_pad)
    # Generous search range so the mass can reach its true extent beyond the sparsely
    # flagged (strided) levels; the seed above decides WHICH component is the mass.
    zg = int(cfg.get("z_search", 45))
    g0 = max(0, min(best) - zg)
    g1 = min(Z - 1, max(best) + zg)

    # 2. solid soft-tissue over the generous range (excludes ascites/fat/bone/contrast).
    region = np.zeros(arr.shape, dtype=bool)
    region[:, :, g0:g1 + 1] = (arr[:, :, g0:g1 + 1] > hu_min) & (arr[:, :, g0:g1 + 1] < hu_max)

    # 3. subtract normal organs (TotalSegmentator). Best-effort — skip on failure.
    organ = _totalseg_organ_mask(volume_path, working_dir, (X, Y, Z))
    if organ is not None:
        region &= ~organ

    # 4. close small gaps + fill enclosed (cystic/necrotic) interior, then connected
    #    components; pick the component that dominates the flagged SEED region and take
    #    its FULL extent (not clipped to the flagged levels).
    if int(cfg.get("close_iters", 2)) > 0:
        region = ndimage.binary_closing(region, iterations=int(cfg.get("close_iters", 2)))
    if bool(cfg.get("fill", True)):
        region = ndimage.binary_fill_holes(region)
    if int(cfg.get("open_iters", 0)) > 0:
        region = ndimage.binary_opening(region, iterations=int(cfg.get("open_iters", 0)))
    lbl, n = ndimage.label(region)
    if n == 0:
        logger.info("abdomen_ct_measure_no_component")
        return None
    seed = np.zeros(arr.shape, dtype=bool)
    seed[:, :, z0:z1 + 1] = True
    seed_counts = ndimage.sum(seed, lbl, index=np.arange(1, n + 1))
    if float(seed_counts.max()) <= 0:
        return None
    comp = int(np.argmax(seed_counts)) + 1
    mask = lbl == comp
    if int(mask.sum()) < min_voxels:
        return None

    # 5. measure the bounding box × spacing (TS=x, AP=y, CC=z).
    xs, ys, zs = np.where(mask)
    ts_mm = (xs.max() - xs.min() + 1) * sx
    ap_mm = (ys.max() - ys.min() + 1) * sy
    cc_mm = (zs.max() - zs.min() + 1) * sz
    n_vox = int(mask.sum())
    volume_ml = n_vox * sx * sy * sz / 1000.0

    result = {
        "ts_mm": round(float(ts_mm), 1),
        "ap_mm": round(float(ap_mm), 1),
        "cc_mm": round(float(cc_mm), 1),
        "volume_ml": round(float(volume_ml), 1),
        "n_voxels": n_vox,
        "z_range": [int(zs.min()), int(zs.max())],
        "spacing_mm": [round(sx, 2), round(sy, 2), round(sz, 2)],
        "organ_exclusion": organ is not None,
        "method": "totalseg_residual_cc_v1",
        "estimate": True,
    }
    logger.info("abdomen_ct_measure_done", **{k: result[k] for k in ("ts_mm", "ap_mm", "cc_mm", "volume_ml", "organ_exclusion")})
    return result


def dominant_mass_seed(
    volume_path: str,
    flagged: list[dict[str, Any]],
    working_dir: str,
    cfg: dict[str, Any] | None = None,
) -> tuple[int, int, int] | None:
    """A seed voxel (x, y, z) inside the dominant flagged mass, for promptable segmentation.

    Thresholding gives a poor BOUNDARY but a fine CENTROID: we take the largest opened
    soft-tissue, non-organ component in the densest flagged cluster and return its
    centroid. Robust fallbacks keep it returning a point whenever a mass was flagged.
    """
    cfg = cfg or {}
    try:
        from scipy import ndimage
    except Exception:
        return None
    if not os.path.exists(volume_path):
        return None

    img = nib.load(volume_path)
    arr = np.squeeze(np.asarray(img.get_fdata(), dtype=np.float32))
    while arr.ndim > 3:
        arr = arr[..., 0]
    if arr.ndim != 3:
        return None
    Z = arr.shape[2]

    mass_z = sorted({int(f["z"]) for f in flagged if f.get("finding") and _MASS_RE.search(str(f["finding"]))}) \
        or sorted({int(f["z"]) for f in flagged if "z" in f})
    if not mass_z:
        return None
    gap = int(cfg.get("cluster_gap", 25))
    clusters: list[list[int]] = [[mass_z[0]]]
    for z in mass_z[1:]:
        (clusters[-1].append(z) if z - clusters[-1][-1] <= gap else clusters.append([z]))
    best = max(clusters, key=len)
    z0 = max(0, min(best) - 2)
    z1 = min(Z - 1, max(best) + 2)

    soft = np.zeros(arr.shape, dtype=bool)
    soft[:, :, z0:z1 + 1] = (arr[:, :, z0:z1 + 1] > float(cfg.get("hu_min", 20.0))) & (
        arr[:, :, z0:z1 + 1] < float(cfg.get("hu_max", 200.0)))
    organ = _totalseg_organ_mask(volume_path, working_dir, arr.shape)
    if organ is not None:
        soft &= ~organ

    opened = ndimage.binary_opening(soft, iterations=2)
    for cand in (opened, soft):  # prefer the eroded core; fall back to raw residual
        lbl, n = ndimage.label(cand)
        if n == 0:
            continue
        counts = np.bincount(lbl.ravel())
        counts[0] = 0
        comp = int(counts.argmax())
        if counts[comp] < int(cfg.get("min_voxels", 50)):
            continue
        # arr axes are (X, Y, Z), so center_of_mass returns (cx, cy, cz) in that order.
        cx, cy, cz = ndimage.center_of_mass(cand, lbl, comp)
        return int(round(cx)), int(round(cy)), int(round(cz))

    # last resort: centre of the flagged region
    return arr.shape[0] // 2, arr.shape[1] // 2, (z0 + z1) // 2


def mass_localization(
    volume_path: str,
    flagged: list[dict[str, Any]],
    working_dir: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Localize the dominant mass for promptable segmentation.

    Returns ``{seed_xyz, core_path, organ_path}``: a seed voxel, plus NIfTI files for
    the mass "core" (the seed's soft-tissue, non-organ residual component — used to
    place multiple positive click prompts across the mass) and the normal-organ mask
    (used to trim leakage). All share the volume grid. None if nothing localizes.
    """
    cfg = cfg or {}
    try:
        from scipy import ndimage
    except Exception:
        return None
    if not os.path.exists(volume_path):
        return None

    img = nib.load(volume_path)
    arr = np.squeeze(np.asarray(img.get_fdata(), dtype=np.float32))
    while arr.ndim > 3:
        arr = arr[..., 0]
    if arr.ndim != 3:
        return None
    Z = arr.shape[2]

    mass_z = sorted({int(f["z"]) for f in flagged if f.get("finding") and _MASS_RE.search(str(f["finding"]))}) \
        or sorted({int(f["z"]) for f in flagged if "z" in f})
    if not mass_z:
        return None
    gap = int(cfg.get("cluster_gap", 25))
    clusters: list[list[int]] = [[mass_z[0]]]
    for z in mass_z[1:]:
        (clusters[-1].append(z) if z - clusters[-1][-1] <= gap else clusters.append([z]))
    best = max(clusters, key=len)
    z0 = max(0, min(best) - int(cfg.get("z_pad", 2)))
    z1 = min(Z - 1, max(best) + int(cfg.get("z_pad", 2)))

    soft = np.zeros(arr.shape, dtype=bool)
    soft[:, :, z0:z1 + 1] = (arr[:, :, z0:z1 + 1] > float(cfg.get("hu_min", 20.0))) & (
        arr[:, :, z0:z1 + 1] < float(cfg.get("hu_max", 200.0)))
    organ = _totalseg_organ_mask(volume_path, working_dir, arr.shape)
    if organ is not None:
        soft &= ~organ

    opened = ndimage.binary_opening(soft, iterations=int(cfg.get("open_iters", 1)))
    lbl, n = ndimage.label(opened)
    if n == 0:
        return None
    counts = np.bincount(lbl.ravel())
    counts[0] = 0
    comp = int(counts.argmax())
    if counts[comp] < int(cfg.get("min_voxels", 50)):
        return None
    core = lbl == comp
    cx, cy, cz = ndimage.center_of_mass(core)
    seed = (int(round(cx)), int(round(cy)), int(round(cz)))

    core_path = os.path.join(working_dir, "mass_core.nii.gz")
    nib.save(nib.Nifti1Image(core.astype(np.uint8), img.affine, img.header), core_path)
    organ_path = None
    if organ is not None:
        organ_path = os.path.join(working_dir, "mass_organ.nii.gz")
        nib.save(nib.Nifti1Image(organ.astype(np.uint8), img.affine, img.header), organ_path)

    logger.info("abdomen_ct_localize", seed=seed, core_voxels=int(core.sum()), organ=organ is not None)
    return {"seed_xyz": seed, "core_path": core_path, "organ_path": organ_path}


def verify_mass(
    volume_path: str,
    flagged: list[dict[str, Any]],
    working_dir: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Grounded 'is the flagged mass real?' verification (option C).

    A CONFABULATED mass sits over normal anatomy: after excluding every normal structure
    TotalSegmentator labels (organs, bowel, muscle, vessels, bone), little coherent
    *unlabeled* soft tissue remains at the flagged location. A REAL mass leaves a large,
    coherent unlabeled soft-tissue component. This grounds the VLM's claim in the voxels
    deterministically instead of re-asking the model.

    Returns ``{verdict, core_volume_ml, coherence, reason, ...}`` where verdict is
    ``verified`` / ``unconfirmed`` / ``unverifiable`` (segmentation missing), or None when
    no mass was claimed (nothing to verify). Reuses the cached TotalSeg mask — no extra GPU.
    """
    cfg = cfg or {}
    try:
        from scipy import ndimage
    except Exception:
        return None
    if not os.path.exists(volume_path):
        return None

    # Only verify when a MASS was actually claimed on the flags.
    mass_z = sorted({
        int(f["z"]) for f in (flagged or [])
        if f.get("finding") and _MASS_RE.search(str(f["finding"]))
    })
    if not mass_z:
        return None

    img = nib.load(volume_path)
    arr = np.squeeze(np.asarray(img.get_fdata(), dtype=np.float32))
    while arr.ndim > 3:
        arr = arr[..., 0]
    if arr.ndim != 3:
        return None
    sx, sy, sz = (float(z) for z in img.header.get_zooms()[:3])
    voxel_ml = sx * sy * sz / 1000.0
    Z = arr.shape[2]

    # Densest flagged cluster → the claimed levels (± a small pad).
    gap = int(cfg.get("cluster_gap", 25))
    clusters: list[list[int]] = [[mass_z[0]]]
    for z in mass_z[1:]:
        (clusters[-1].append(z) if z - clusters[-1][-1] <= gap else clusters.append([z]))
    best = max(clusters, key=len)
    pad = int(cfg.get("verify_z_pad", 10))
    z0 = max(0, min(best) - pad)
    z1 = min(Z - 1, max(best) + pad)

    hu_min = float(cfg.get("hu_min", 20.0))
    hu_max = float(cfg.get("hu_max", 200.0))
    soft = np.zeros(arr.shape, dtype=bool)
    soft[:, :, z0:z1 + 1] = (arr[:, :, z0:z1 + 1] > hu_min) & (arr[:, :, z0:z1 + 1] < hu_max)

    organ = _totalseg_organ_mask(volume_path, working_dir, arr.shape)
    if organ is None:
        # Can't exclude normal structures → can't verify. Don't claim either way.
        return {"verdict": "unverifiable", "reason": "organ segmentation unavailable"}

    nonorgan = soft & ~organ
    nonorgan_vox = int(nonorgan.sum())
    opened = ndimage.binary_opening(nonorgan, iterations=int(cfg.get("open_iters", 1)))
    lbl, n = ndimage.label(opened)

    min_ml = float(cfg.get("verify_min_ml", 20.0))
    min_coh = float(cfg.get("verify_min_coherence", 0.25))

    if n == 0:
        return {
            "verdict": "unconfirmed", "core_volume_ml": 0.0, "coherence": 0.0,
            "z_range": [z0, z1], "min_ml": min_ml, "min_coherence": min_coh,
            "reason": "no coherent unlabeled soft-tissue component at the flagged location",
            "method": "totalseg_residual_verify_v1",
        }

    counts = np.bincount(lbl.ravel())
    counts[0] = 0
    comp = int(counts.argmax())
    core_vox = int(counts[comp])
    core = lbl == comp
    core_volume_ml = core_vox * voxel_ml
    coherence = core_vox / max(nonorgan_vox, 1)  # mass dominates residual, vs scattered
    xs, ys, zs = np.where(core)
    bbox = (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1) * (zs.max() - zs.min() + 1)
    fill = core_vox / max(bbox, 1)

    verified = core_volume_ml >= min_ml and coherence >= min_coh
    result = {
        "verdict": "verified" if verified else "unconfirmed",
        "core_volume_ml": round(core_volume_ml, 1),
        "coherence": round(float(coherence), 2),
        "fill": round(float(fill), 2),
        "nonorgan_ml": round(nonorgan_vox * voxel_ml, 1),
        "z_range": [z0, z1],
        "min_ml": min_ml, "min_coherence": min_coh,
        "reason": (
            "a coherent unlabeled soft-tissue mass of sufficient size is present"
            if verified else
            "the flagged region is largely normal/organ tissue — no coherent unlabeled "
            "mass of sufficient size"
        ),
        "method": "totalseg_residual_verify_v1",
    }
    logger.info(
        "abdomen_ct_verify_mass", verdict=result["verdict"],
        core_volume_ml=result["core_volume_ml"], coherence=result["coherence"],
    )
    return result


def _totalseg_organ_mask(volume_path: str, working_dir: str, shape: tuple[int, int, int]) -> np.ndarray | None:
    """Binary mask of every normal structure TotalSegmentator labels (label > 0)."""
    import subprocess

    try:
        import torch

        device = "gpu" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"

    out_dir = os.path.join(working_dir, "abdomen_measure_seg")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "organs.nii.gz")
    if not os.path.exists(out):  # cache: TotalSeg is the slow step, reuse across calls
        cmd = [
            "TotalSegmentator", "-i", str(volume_path), "-o", out,
            "-ta", "total", "--ml", "--fast", "-d", device,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as exc:
            logger.warning("abdomen_ct_measure_totalseg_failed", error=str(exc))
            return None
        if proc.returncode != 0 or not os.path.exists(out):
            logger.warning("abdomen_ct_measure_totalseg_rc", rc=proc.returncode, stderr=(proc.stderr or "")[-400:])
            return None

    seg = np.squeeze(np.asarray(nib.load(out).get_fdata()))
    while seg.ndim > 3:
        seg = seg[..., 0]
    if seg.shape != tuple(shape):
        logger.warning("abdomen_ct_measure_seg_shape", got=tuple(seg.shape), expected=tuple(shape))
        return None
    return seg > 0.5
