#!/usr/bin/env python3
"""Download the AutoPET3 (Team LesionTracer) final checkpoint from Zenodo.

The autopet3 submodule ships only the code; the trained 5-fold checkpoint lives
on Zenodo (record 14007247). This helper fetches the record's files into a
target MODEL_FOLDER and unzips any archives, so the result is a directory
containing fold_0 ... fold_4 (plus dataset.json / plans.json) suitable for
pet_ct's `autopet3.model_dir`.

    python scripts/download_autopet3_weights.py --output /model_cache/autopet3

Then in pet_ct/model/inference_config.yaml set:
    model.autopet3.enabled: true
    model.autopet3.model_dir: /model_cache/autopet3/<the model folder>

Note: the checkpoint is large (multiple GB). Requires outbound network access.
"""
from __future__ import annotations

import argparse
import os
import zipfile

import requests

ZENODO_RECORD = "14007247"
ZENODO_API = f"https://zenodo.org/api/records/{ZENODO_RECORD}"


def _download(url: str, dest: str) -> None:
    print(f"  downloading {os.path.basename(dest)} ...", flush=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download AutoPET3 checkpoint from Zenodo")
    parser.add_argument("--output", required=True, help="target MODEL_FOLDER directory")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)
    print(f"Querying Zenodo record {ZENODO_RECORD} ...", flush=True)
    meta = requests.get(ZENODO_API, timeout=60).json()
    files = meta.get("files", [])
    if not files:
        print("No files found on the Zenodo record — check the record id / access.")
        return 1

    for f in files:
        name = f.get("key") or f.get("filename")
        url = (f.get("links") or {}).get("self") or (f.get("links") or {}).get("download")
        if not name or not url:
            continue
        dest = os.path.join(args.output, name)
        _download(url, dest)
        if name.endswith(".zip"):
            print(f"  unzipping {name} ...", flush=True)
            with zipfile.ZipFile(dest) as z:
                z.extractall(args.output)
            os.remove(dest)

    # Locate the actual nnU-Net MODEL_FOLDER (the dir containing fold_X subdirs).
    model_folder = None
    for cur, dirs, _ in os.walk(args.output):
        if any(d.startswith("fold_") for d in dirs):
            model_folder = cur
            break

    print(f"\nDone. Extracted under: {args.output}")
    if model_folder:
        print(f"MODEL_FOLDER (contains fold_X): {model_folder}")
        if os.path.abspath(model_folder) == os.path.abspath(args.output):
            print("→ model.autopet3.model_dir is already correct.")
        else:
            print("→ Set model.autopet3.model_dir to the path above (or its parent — "
                  "the pipeline auto-descends).")
    else:
        print("WARNING: no fold_X folder found — inspect the extracted contents and set "
              "model.autopet3.model_dir to the folder containing fold_0 ... fold_4.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
