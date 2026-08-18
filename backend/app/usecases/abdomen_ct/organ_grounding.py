from __future__ import annotations

"""Tier-1 deterministic organ grounding for abdomen_ct.

A VLM cannot measure and over-/under-calls organ size (it missed a 15.5 cm hepatomegaly
while confabulating a mass). This module derives OBJECTIVE organ sizes with a purpose-
built model instead: it runs TotalSegmentator (already a worker dependency), measures
each major organ from its 3-D mask, and emits size-based findings (hepatomegaly,
splenomegaly, aortic aneurysm). These are deterministic FACTS — no language model, no
hallucination.

They are used two ways by the Celery hook:
  1. Surfaced in ``summary`` (``organ_measurements`` / ``organ_findings``).
  2. Fed to the report-writer as AUTHORITATIVE ground truth, so it (a) reports the
     organomegaly the VLM cannot perceive and (b) is anchored — it must not describe an
     organ as enlarged, or a mass arising from an organ, that is measured NORMAL.

Age-aware where norms differ (paediatric vs adult); measurements are always surfaced,
flags are conservative and labelled AI-estimated. Reuses the same cached TotalSeg mask
as ``measurement.py`` (``working_dir/abdomen_measure_seg/organs.nii.gz``). Non-blocking:
returns None on any failure so the report simply omits the measurements.
"""

import os
from typing import Any

import nibabel as nib
import numpy as np
import structlog

logger = structlog.get_logger(__name__)


def _label_index_map() -> dict[str, int]:
    """TotalSegmentator 'total' name→label-index map (from the installed package; falls
    back to the stable core indices shared by v1/v2 if the import is unavailable)."""
    try:
        from totalsegmentator.map_to_binary import class_map

        cm = class_map.get("total") or {}
        m = {str(name): int(idx) for idx, name in cm.items()}
        if m:
            return m
    except Exception as exc:  # pragma: no cover - depends on installed version
        logger.warning("organ_grounding_classmap_unavailable", error=str(exc))
    # Core organ indices are stable across TotalSegmentator v1/v2; aorta omitted (its
    # index moved between versions, so only trust it when class_map is present).
    return {"spleen": 1, "kidney_right": 2, "kidney_left": 3, "liver": 5}


def _run_totalseg_ml(volume_path: str, working_dir: str) -> str | None:
    """Path to the multilabel TotalSeg mask (shared cache with measurement.py)."""
    import subprocess

    try:
        import torch

        device = "gpu" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"

    out_dir = os.path.join(working_dir, "abdomen_measure_seg")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "organs.nii.gz")
    if not os.path.exists(out):  # cache: TotalSeg is the slow step; measurement reuses it
        cmd = [
            "TotalSegmentator", "-i", str(volume_path), "-o", out,
            "-ta", "total", "--ml", "--fast", "-d", device,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as exc:
            logger.warning("organ_grounding_totalseg_failed", error=str(exc))
            return None
        if proc.returncode != 0 or not os.path.exists(out):
            logger.warning(
                "organ_grounding_totalseg_rc", rc=proc.returncode,
                stderr=(proc.stderr or "")[-300:],
            )
            return None
    return out


def analyze_organs(
    volume_path: str,
    working_dir: str,
    *,
    age_years: int | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Measure the major abdominal organs → ``{measurements, findings, facts_text}`` or None."""
    cfg = cfg or {}
    if not os.path.exists(volume_path):
        return None
    seg_path = _run_totalseg_ml(volume_path, working_dir)
    if not seg_path:
        return None

    img = nib.load(volume_path)
    sx, sy, sz = (float(z) for z in img.header.get_zooms()[:3])
    voxel_ml = sx * sy * sz / 1000.0
    seg = np.squeeze(np.asarray(nib.load(seg_path).get_fdata()))
    while seg.ndim > 3:
        seg = seg[..., 0]
    idx = _label_index_map()

    def _mask(name: str) -> np.ndarray | None:
        i = idx.get(name)
        if not i:
            return None
        m = seg == i
        return m if m.any() else None

    def _measure(name: str) -> dict[str, Any] | None:
        m = _mask(name)
        if m is None:
            return None
        xs, ys, zs = np.where(m)
        dims = [
            (xs.max() - xs.min() + 1) * sx,
            (ys.max() - ys.min() + 1) * sy,
            (zs.max() - zs.min() + 1) * sz,
        ]
        return {
            "volume_ml": round(int(m.sum()) * voxel_ml, 0),
            "cc_mm": round(dims[2], 0),
            "max_dim_mm": round(max(dims), 0),
        }

    meas: dict[str, Any] = {}
    for o in ("liver", "spleen", "kidney_left", "kidney_right"):
        r = _measure(o)
        if r:
            meas[o] = r

    # Abdominal aorta diameter — MINOR-axis per axial slice, sustained over a real
    # segment. Equivalent-area diameter over-measures wherever the aorta runs obliquely
    # (an oblique cut through the tube is an ellipse whose area is inflated); the ellipse
    # MINOR axis ≈ the true lumen diameter regardless of obliquity. We take the diameter
    # sustained over ≥~5 mm of the vessel (not a single artefact slice).
    am = _mask("aorta")
    if am is not None:
        diams: list[float] = []
        for z in range(am.shape[2]):
            sl = am[:, :, z]
            if int(sl.sum()) < 8:  # too few voxels to trust a shape
                continue
            xs, ys = np.where(sl)
            pts = np.stack([xs * sx, ys * sy], axis=1).astype(np.float64)  # mm coords
            pts -= pts.mean(axis=0)
            cov = (pts.T @ pts) / len(pts)
            evals = np.linalg.eigvalsh(cov)  # ascending; evals[0] = minor-axis variance
            diams.append(4.0 * float(np.sqrt(max(evals[0], 0.0))))  # solid-ellipse minor Ø
        if diams:
            ds = np.sort(np.array(diams))[::-1]
            k = max(3, int(round(5.0 / max(sz, 1e-3))))  # ~5 mm of contiguous vessel
            sustained = float(ds[min(k - 1, len(ds) - 1)])
            meas["aorta"] = {"max_diameter_mm": round(sustained, 0), "method": "minor_axis"}

    findings = _size_findings(meas, age_years)
    facts_text = _facts_text(meas, findings)
    logger.info(
        "organ_grounding_done", organs=list(meas), flags=len(findings),
        age_years=age_years,
    )
    return {"measurements": meas, "findings": findings, "facts_text": facts_text, "estimate": True}


def _size_findings(meas: dict[str, Any], age_years: int | None) -> list[str]:
    """Conservative, age-aware size flags. Absence of a flag ≠ normal — it means 'not
    enlarged beyond the threshold by measurement'."""
    out: list[str] = []
    peds = age_years is not None and age_years < 15
    age = int(age_years or 0)

    # Hepatomegaly on SPAN (craniocaudal extent) OR volume — a long liver enlarges the
    # span before the volume crosses a threshold (that is how it is called clinically and
    # why a volume-only rule missed a 15.9 cm paediatric liver). Age-calibrated, coarse,
    # AI-estimated: correlate with age-specific nomograms.
    liver = meas.get("liver") or {}
    lv, lc = liver.get("volume_ml"), liver.get("cc_mm")
    if lv is not None:
        span_uln = (100 + 4 * age) if peds else 180.0     # mm craniocaudal ULN (proxy)
        vol_uln = ((250 + 70 * age) * 1.5) if peds else 2000.0
        reasons: list[str] = []
        if lc is not None and lc > span_uln:
            reasons.append(f"craniocaudal span ~{lc:.0f} mm (age-expected ≤ ~{span_uln:.0f} mm)")
        if lv > vol_uln:
            reasons.append(f"volume ~{lv:.0f} mL")
        if reasons:
            out.append("Hepatomegaly — the liver is enlarged (" + "; ".join(reasons) + ").")

    sp = meas.get("spleen") or {}
    sv, sl = sp.get("volume_ml"), sp.get("max_dim_mm")
    if sv is not None:
        if peds and sl and sl > 120:
            out.append(f"Splenomegaly — the spleen is enlarged (length ~{sl:.0f} mm).")
        elif not peds and (sv > 350 or (sl and sl > 130)):
            out.append(f"Splenomegaly — the spleen is enlarged (measured volume ~{sv:.0f} mL).")

    ad = (meas.get("aorta") or {}).get("max_diameter_mm")
    if ad is not None and not peds and ad > 30:
        out.append(f"Aortic aneurysm — the abdominal aorta is dilated (max diameter ~{ad:.0f} mm).")

    return out


def _facts_text(meas: dict[str, Any], findings: list[str]) -> str:
    lines: list[str] = []
    label = {
        "liver": "Liver", "spleen": "Spleen",
        "kidney_right": "Right kidney", "kidney_left": "Left kidney",
    }
    for k in ("liver", "spleen", "kidney_right", "kidney_left"):
        if k in meas:
            extra = ""
            if k == "liver" and meas[k].get("cc_mm"):
                extra = f", craniocaudal span ~{meas[k]['cc_mm']:.0f} mm"
            lines.append(f"- {label[k]}: ~{meas[k]['volume_ml']:.0f} mL{extra}")
    if "aorta" in meas:
        lines.append(f"- Abdominal aorta, max diameter: ~{meas['aorta']['max_diameter_mm']:.0f} mm")
    if not lines:
        return ""
    flagged = "; ".join(findings) if findings else "no size abnormality detected by measurement"
    return (
        "MEASURED ORGAN SIZES (automated 3-D segmentation — authoritative):\n"
        + "\n".join(lines)
        + f"\nSize assessment: {flagged}"
    )
