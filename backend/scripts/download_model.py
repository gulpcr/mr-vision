#!/usr/bin/env python3
"""Pre-download the BraTS brain segmentation model from the MONAI Model Zoo.

Run this before first inference to avoid download latency during a pipeline run:

    python scripts/download_model.py

Or inside Docker:

    docker compose exec worker python scripts/download_model.py

The model is cached at the path configured in inference_config.yaml and persisted
via Docker volume so subsequent container restarts skip the download.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monai.bundle import download


def main():
    cache_dir = Path(__file__).resolve().parent.parent / "app" / "usecases" / "brain_mri" / "model" / "bundles"
    cache_dir.mkdir(parents=True, exist_ok=True)

    marker = cache_dir / "brats_mri_segmentation" / ".download_complete"
    if marker.exists():
        print(f"Model already downloaded at {cache_dir / 'brats_mri_segmentation'}")
        models_dir = cache_dir / "brats_mri_segmentation" / "models"
        if models_dir.exists():
            for f in models_dir.iterdir():
                size_mb = f.stat().st_size / (1024 * 1024)
                print(f"  {f.name}: {size_mb:.1f} MB")
        return

    print(f"Downloading brats_mri_segmentation bundle to {cache_dir} ...")
    download(
        name="brats_mri_segmentation",
        bundle_dir=str(cache_dir),
    )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok")

    print("Download complete.")
    models_dir = cache_dir / "brats_mri_segmentation" / "models"
    if models_dir.exists():
        for f in models_dir.iterdir():
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"  {f.name}: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
