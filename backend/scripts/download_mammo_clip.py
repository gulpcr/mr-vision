#!/usr/bin/env python3
"""Stage the Mammo-CLIP B5 weights for the mammography structured-findings slots.

The inference code is vendored in-repo at backend/external/Mammo-CLIP/breastclip
(+ mammo_clip_adapter.py), so this script only downloads the released B5 checkpoint
(HF `shawn24/Mammo-CLIP`) into the weights directory
(Settings.mammography_clip_weights_path, default /model_cache/mammo_clip).

Run before enabling MAMMOGRAPHY_CLIP_ENABLED:

    docker compose exec worker python scripts/download_mammo_clip.py

License: Mammo-CLIP is CC BY-NC-SA 4.0 (non-commercial). See external/Mammo-CLIP/README.md.
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HF_REPO = "shawn24/Mammo-CLIP"
CKPT_IN_REPO = "Pre-trained-checkpoints/b5-model-best-epoch-7.tar"
DET_IN_REPO = "Downstream-checkpoints/Mammo-CLIP-Detection.zip"
EXT_DIR = Path(__file__).resolve().parent.parent / "external" / "Mammo-CLIP"


def _weights_dir() -> Path:
    try:
        from app.config import get_settings

        return Path(get_settings().mammography_clip_weights_path)
    except Exception:
        return Path("/model_cache/mammo_clip")


def _detector_dir() -> Path:
    try:
        from app.config import get_settings

        return Path(get_settings().mammography_detector_weights_path)
    except Exception:
        return Path("/model_cache/mammo_detector")


def check_code() -> bool:
    ok = (EXT_DIR / "breastclip").is_dir() and (EXT_DIR / "mammo_clip_adapter.py").is_file()
    print(f"[code] vendored breastclip present: {ok} ({EXT_DIR})")
    if not ok:
        print("[code] ERROR: vendored Mammo-CLIP code missing from the repo — cannot proceed.")
    return ok


def download_weights() -> None:
    wdir = _weights_dir()
    wdir.mkdir(parents=True, exist_ok=True)
    target = wdir / Path(CKPT_IN_REPO).name
    if target.exists():
        print(f"[weights] present: {target.name} ({target.stat().st_size / 1e6:.0f} MB)")
        return
    try:
        from huggingface_hub import hf_hub_download
    except Exception:
        print("[weights] huggingface_hub not installed. Install it, or manually place the")
        print(f"          B5 checkpoint from https://huggingface.co/{HF_REPO} into {wdir}")
        return
    print(f"[weights] downloading {CKPT_IN_REPO} ...")
    path = hf_hub_download(repo_id=HF_REPO, filename=CKPT_IN_REPO)
    shutil.copyfile(path, target)
    print(f"[weights] -> {target} ({target.stat().st_size / 1e6:.0f} MB)")


def download_detectors() -> None:
    """Download + extract the released Mass and Suspicious Calcification RetinaNet
    detectors (unfrozen-backbone variant) into the detector weights dir."""
    import glob
    import zipfile

    ddir = _detector_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    if glob.glob(str(ddir / "*_n" / "**" / "*.pth"), recursive=True):
        print(f"[detector] present in {ddir}")
        return
    try:
        from huggingface_hub import hf_hub_download
    except Exception:
        print(f"[detector] huggingface_hub missing; place {DET_IN_REPO} contents into {ddir}")
        return
    print(f"[detector] downloading {DET_IN_REPO} ...")
    outer = hf_hub_download(repo_id=HF_REPO, filename=DET_IN_REPO)
    with zipfile.ZipFile(outer) as z:
        z.extractall(ddir)
    # extract the unfrozen-backbone Mass + Calcification detectors
    for inner in glob.glob(str(ddir / "*.zip")):
        name = os.path.basename(inner)
        if "freeze_backbone_n" not in name:
            continue
        sub = "mass_n" if "concepts_Mass_" in name else ("calc_n" if "Suspicious Calcification" in name else None)
        if sub:
            with zipfile.ZipFile(inner) as z:
                z.extractall(ddir / sub)
            print(f"[detector] extracted -> {sub}")


def main() -> None:
    if not check_code():
        sys.exit(1)
    download_weights()
    download_detectors()
    print("Done. Set MAMMOGRAPHY_CLIP_ENABLED / MAMMOGRAPHY_DETECTOR_ENABLED=true and restart the worker.")


if __name__ == "__main__":
    main()
