from __future__ import annotations

"""Shared, persistent TotalSegmentator predictor.

Previously every caller (``usecases/abdomen_ct/measurement.py`` and
``usecases/abdomen_ct/organ_grounding.py``) shelled out to the ``TotalSegmentator`` CLI
via ``subprocess.run(...)`` — a brand-new OS process every call, which re-reads the
nnU-Net checkpoint from disk and rebuilds the network from scratch every single time.
That is the opposite of "load once, reuse for concurrent requests."

This module monkey-patches ``totalsegmentator.nnunet.nnUNetPredictor`` so that
``initialize_from_trained_model_folder`` (the disk-I/O-heavy step: reads
``dataset.json``/``plans.json`` and every fold's checkpoint via ``torch.load``, then
builds the network and loads its state dict) runs ONCE per (model folder, folds,
checkpoint) and is cached; every later call reuses the already-built, already-on-GPU
network object instead of rebuilding it. We still call the public, well-tested
``totalsegmentator.python_api.totalsegmentator(...)`` entry point for everything else
(preprocessing, resampling, cropping, saving) — only the model-loading step is patched.

Thread-safety: because the cached ``network`` module object is SHARED across every
caller after the first load, we do not allow two threads to run inference against it
at the same time (nnU-Net's own predict path can, depending on config, swap per-fold
weights into the same network object in place — safe only if serialized). ``_INFER_LOCK``
wraps the entire ``totalsegmentator(...)`` call, not just the weight-loading step, so
preprocessing for job B can still run on CPU while job A's segmentation is on the GPU,
but only one job is ever inside the actual TotalSegmentator call at a time. This is the
same "one loaded model, callers queue for the forward pass" design as
``sammed3d_engine._INFER_LOCK``, and is exactly the number this platform's GPU-capacity
test is trying to measure: how much serialization one resident model imposes on N
concurrent CT report jobs.
"""

import os
import threading
from typing import Any

import nibabel as nib
import numpy as np
import structlog

logger = structlog.get_logger(__name__)

_PATCH_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()
_PATCHED = False
_PREDICTOR_CACHE: dict[tuple, Any] = {}


def _patch_once() -> None:
    """Install the caching monkey-patch on first use. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return
    with _PATCH_LOCK:
        if _PATCHED:
            return
        import totalsegmentator.nnunet as tsnn

        _RealPredictor = tsnn.nnUNetPredictor
        _cache = _PREDICTOR_CACHE
        _cache_lock = threading.Lock()

        _CACHED_ATTRS = (
            "plans_manager", "configuration_manager", "list_of_parameters",
            "network", "dataset_json", "trainer_name", "allowed_mirroring_axes",
            "label_manager",
        )

        class _CachingPredictor(_RealPredictor):
            def initialize_from_trained_model_folder(self, model_training_output_dir, use_folds,
                                                       checkpoint_name="checkpoint_final.pth"):
                key = (str(model_training_output_dir), tuple(use_folds) if use_folds else None,
                       checkpoint_name, str(self.device))
                with _cache_lock:
                    cached = _cache.get(key)
                    if cached is not None:
                        for attr in _CACHED_ATTRS:
                            setattr(self, attr, getattr(cached, attr))
                        logger.info("totalseg_predictor_cache_hit", model_folder=os.path.basename(
                            str(model_training_output_dir)))
                        return
                    super().initialize_from_trained_model_folder(
                        model_training_output_dir, use_folds, checkpoint_name
                    )
                    _cache[key] = self
                    logger.info("totalseg_predictor_loaded", model_folder=os.path.basename(
                        str(model_training_output_dir)))

        tsnn.nnUNetPredictor = _CachingPredictor
        _PATCHED = True


def ensure_segmentation(
    volume_path: str,
    out_path: str,
    device: str = "gpu",
    task: str = "total",
) -> str | None:
    """Ensure the multilabel TotalSegmentator mask exists at ``out_path``, return its path.

    Drop-in replacement for the old per-caller ``_totalseg_organ_mask``/
    ``_run_totalseg_ml`` subprocess helpers — same on-disk-per-job cache semantics
    (skip the call entirely if ``out_path`` already exists), but the model itself now
    stays resident in this process across every job instead of reloading from disk.
    Callers interpret the multilabel file however they need (boolean any-organ mask,
    or per-label lookup) — this function only guarantees the file is there.
    """
    if os.path.exists(out_path):
        return out_path
    try:
        _patch_once()
        from totalsegmentator.python_api import totalsegmentator

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with _INFER_LOCK:
            # nr_thr_saving defaults to 6, which spawns multiprocessing.Pool(6) —
            # i.e. fork() — inside nnunet.py. Forking a process that already has an
            # initialized CUDA context (this worker runs a --pool=threads Celery pool,
            # so CUDA is live in this same OS process from other threads/calls) crashes
            # or hangs the child and has been observed to kill the whole worker
            # MainProcess. nr_thr_saving=1 keeps segmentation export in-process/serial —
            # no fork, no multiprocessing. Same for nr_thr_resamp (already defaults to 1).
            totalsegmentator(
                input=volume_path, output=out_path, task=task,
                ml=True, fast=True, device=device, quiet=True,
                nr_thr_resamp=1, nr_thr_saving=1,
            )
    except Exception as exc:
        logger.warning("totalseg_engine_failed", error=str(exc))
        return None
    if not os.path.exists(out_path):
        logger.warning("totalseg_engine_no_output", out=out_path)
        return None
    return out_path


def get_organ_mask(
    volume_path: str,
    out_path: str,
    shape: tuple[int, int, int],
    device: str = "gpu",
    task: str = "total",
) -> np.ndarray | None:
    """Binary mask of every normal structure TotalSegmentator labels (label > 0)."""
    seg_path = ensure_segmentation(volume_path, out_path, device=device, task=task)
    if not seg_path:
        return None

    seg = np.squeeze(np.asarray(nib.load(seg_path).get_fdata()))
    while seg.ndim > 3:
        seg = seg[..., 0]
    if seg.shape != tuple(shape):
        logger.warning("totalseg_engine_shape_mismatch", got=tuple(seg.shape), expected=tuple(shape))
        return None
    return seg > 0.5
