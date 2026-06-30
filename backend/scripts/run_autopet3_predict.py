#!/usr/bin/env python3
"""Isolated AutoPET3 (Team LesionTracer) nnU-Net inference runner.

Run OUT-OF-PROCESS by the pet_ct pipeline so the AutoPET3 nnU-Net *fork*
(backend/external/autopet3, which ships the custom ``autoPET3_Trainer`` and
ResEncL plans the checkpoint was trained with) shadows the pip-installed
``nnunetv2`` used by TotalSegmentator. Importing the fork in-process would risk
breaking TotalSegmentator — hence this separate process.

Usage:
    python run_autopet3_predict.py \
        --input  <dir with case_0000.nii.gz (CT), case_0001.nii.gz (PET/SUV)> \
        --output <dir for the predicted lesion mask> \
        --model  <MODEL_FOLDER containing fold_X subfolders (Zenodo 14007247)> \
        --fork   <path to backend/external/autopet3> \
        --folds  0,1,2,3,4 \
        --checkpoint checkpoint_final.pth \
        --device cuda|cpu

The fork path is prepended to sys.path so ``import nnunetv2`` resolves to it.
Mirrors nnUNetv2_predict_from_modelfolder (predict_entry_point_modelfolder).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated AutoPET3 nnU-Net inference")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fork", required=True, help="path to the autopet3 nnU-Net fork")
    parser.add_argument("--folds", default="0,1,2,3,4")
    parser.add_argument("--checkpoint", default="checkpoint_final.pth")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu", "mps"])
    parser.add_argument("--step_size", type=float, default=0.5)
    args = parser.parse_args()

    # Make the AutoPET3 fork the importable `nnunetv2` for THIS process only.
    fork = str(Path(args.fork).resolve())
    if fork not in sys.path:
        sys.path.insert(0, fork)

    import torch

    # torch >= 2.6 defaults torch.load(weights_only=True), which rejects the
    # AutoPET3 nnU-Net checkpoints (they pickle the trainer/plans/init_args, not
    # just tensors). The fork's predict_from_raw_data.py calls bare torch.load,
    # so make weights_only=False the default for THIS process only. The fork is
    # a vendored submodule we must not edit; the checkpoint source (Zenodo
    # 14007247, Team LesionTracer) is trusted.
    _orig_torch_load = torch.load

    def _trusting_load(*a, **kw):
        kw.setdefault("weights_only", False)
        return _orig_torch_load(*a, **kw)

    torch.load = _trusting_load

    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

    folds = tuple(f if f == "all" else int(f) for f in args.folds.split(","))

    device = torch.device(args.device)
    if args.device == "cpu":
        import multiprocessing

        torch.set_num_threads(multiprocessing.cpu_count())
    elif args.device == "cuda":
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)

    predictor = nnUNetPredictor(
        tile_step_size=args.step_size,
        use_gaussian=True,
        use_mirroring=True,
        perform_everything_on_device=True,
        device=device,
        verbose=False,
        allow_tqdm=False,
    )
    predictor.initialize_from_trained_model_folder(args.model, folds, args.checkpoint)
    predictor.predict_from_files(
        args.input,
        args.output,
        save_probabilities=False,
        overwrite=True,
        num_processes_preprocessing=3,
        num_processes_segmentation_export=3,
        folder_with_segs_from_prev_stage=None,
        num_parts=1,
        part_id=0,
    )
    print("autopet3_predict_done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
