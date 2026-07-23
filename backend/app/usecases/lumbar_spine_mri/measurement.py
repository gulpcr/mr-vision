from __future__ import annotations

"""Level-identification pass for lumbar_spine_mri — segmentation-verified vertebral levels.

The MedGemma VLM cannot reliably localize levels (it labeled the same slice L4-L5 on one
view and L5-S1 on another, and drifted between L3-L4/L4-L5 across adjacent axials). This
module runs TotalSegmentator's MR-native ``vertebrae_mr`` model, which labels each lumbar
vertebra (L1–L5) and the sacrum individually, and emits a factual statement of WHICH levels
were imaged and resolved — an objective scaffold the narrative can be read against.

Scope is deliberately LIMITED to level identification. It does NOT grade spondylolisthesis,
stenosis, or alignment: on thick screening MR (~15 sagittal slices, 4.8 mm) those grades
could not be derived reliably from region masks (validation produced systematic false
high-grade slips at L5-S1, including on near-normal spines), and doing them properly needs a
vertebral-corner landmark model + a validation set with ground-truth grades. Until then this
ships the one thing that IS reliable. Severity stays with the radiologist / narrative pass.

Self-contained by contract: no infrastructure/interface imports, no file I/O beyond the
TotalSegmentator subprocess it drives in the working dir. Fully NON-BLOCKING — returns None
on any failure so the report is simply unchanged. TotalSegmentator runs as a SUBPROCESS
(never the Python API): the Celery worker is daemonic and nnU-Net's child pool cannot be
spawned from it (same reason the CT pipelines shell out). NON-DIAGNOSTIC assistive output.
"""

import os
import subprocess
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Lumbar vertebrae, superior→inferior. Disc interspaces close with L5-S1.
_LUMBAR = ["L1", "L2", "L3", "L4", "L5"]


def _device() -> str:
    try:
        import torch
        return "gpu" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _run_totalseg(volume_path: str, out_path: str, task: str, timeout_s: int) -> bool:
    """Run one TotalSegmentator task as a multilabel subprocess; cache by output path."""
    if os.path.exists(out_path):
        return True
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = [
        "TotalSegmentator", "-i", str(volume_path), "-o", str(out_path),
        "-ta", task, "--ml", "-d", _device(),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except Exception as exc:
        logger.warning("lumbar_measure_totalseg_failed", task=task, error=str(exc))
        return False
    if proc.returncode != 0 or not os.path.exists(out_path):
        logger.warning(
            "lumbar_measure_totalseg_rc", task=task, rc=proc.returncode,
            stderr=(proc.stderr or "")[-400:],
        )
        return False
    return True


def _levels_phrase(lumbar: list[str], sacrum: bool) -> str:
    """Compact human phrase for a contiguous (or not) set of identified lumbar levels."""
    if not lumbar:
        return "no lumbar vertebral levels" + (" (sacrum only)" if sacrum else "")
    order = {n: i for i, n in enumerate(_LUMBAR)}
    idx = sorted(order[l] for l in lumbar)
    contiguous = idx == list(range(idx[0], idx[-1] + 1))
    span = f"{_LUMBAR[idx[0]]}-{_LUMBAR[idx[-1]]}" if contiguous and len(idx) > 1 else ", ".join(
        _LUMBAR[i] for i in idx
    )
    tail = " and the S1/sacrum" if sacrum else ""
    return f"{span}{tail}"


def measure_levels(volume_path: str, working_dir: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Identify the lumbar vertebral levels on the sagittal volume, or return None.

    Returns ``{levels_present, lumbar_levels, sacrum_present, partial_levels, disc_levels,
    standard_coverage, measured_findings, method}``. ``measured_findings`` is a ready-to-inject
    factual sentence stating which levels were imaged/resolved (and any only partially covered
    at the field-of-view edge). No grading is performed.
    """
    import nibabel as nib
    import numpy as np

    try:
        vtask = str(cfg.get("vertebra_task", "vertebrae_mr"))
        timeout_s = int(cfg.get("timeout_s", 1800))
        min_vox = int(cfg.get("min_level_voxels", 1500))     # solidly resolved
        partial_vox = int(cfg.get("partial_level_voxels", 150))  # partially imaged (FOV edge)

        seg_dir = os.path.join(working_dir, "lumbar_measure_seg")
        vpath = os.path.join(seg_dir, "vertebrae.nii.gz")
        if not _run_totalseg(volume_path, vpath, vtask, timeout_s):
            return None

        from totalsegmentator.map_to_binary import class_map
        vmap = {v: k for k, v in class_map[vtask].items()}   # name → label id
        arr = np.asarray(nib.as_closest_canonical(nib.load(vpath)).get_fdata()).astype(int)

        def _count(name: str) -> int:
            lid = vmap.get(f"vertebrae_{name}") if name in _LUMBAR else vmap.get(name)
            return 0 if lid is None else int((arr == lid).sum())

        lumbar = [n for n in _LUMBAR if _count(n) >= min_vox]
        partial = [n for n in _LUMBAR if partial_vox <= _count(n) < min_vox]
        sacrum = _count("sacrum") >= min_vox
        if not lumbar and not sacrum:
            return None

        present = list(lumbar) + (["S1"] if sacrum else [])
        # Disc interspaces derivable from contiguous identified vertebrae (+ L5-S1).
        disc_levels: list[str] = []
        for a, b in zip(_LUMBAR, _LUMBAR[1:]):
            if a in lumbar and b in lumbar:
                disc_levels.append(f"{a}-{b}")
        if "L5" in lumbar and sacrum:
            disc_levels.append("L5-S1")

        standard = lumbar == _LUMBAR and sacrum
        phrase = _levels_phrase(lumbar, sacrum)
        if standard:
            measured = (
                "Segmentation confirmed the standard five lumbar levels (L1-L5) and the "
                "S1/sacrum; disc levels L1-L2 through L5-S1 are imaged."
            )
        else:
            measured = f"Segmentation identified {phrase}."
            if partial:
                measured += (
                    " " + (", ".join(partial)) + (" was" if len(partial) == 1 else " were")
                    + " only partially imaged at the field-of-view edge."
                )

        result = {
            "levels_present": present,
            "lumbar_levels": lumbar,
            "sacrum_present": sacrum,
            "partial_levels": partial,
            "disc_levels": disc_levels,
            "standard_coverage": standard,
            "measured_findings": measured,
            "method": f"totalsegmentator({vtask}) level identification",
        }
        logger.info(
            "lumbar_measure_complete", levels=present, partial=partial, standard=standard,
        )
        return result
    except Exception as exc:
        logger.warning("lumbar_measure_failed", error=str(exc))
        return None
