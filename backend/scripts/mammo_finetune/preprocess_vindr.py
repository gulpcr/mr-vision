#!/usr/bin/env python3
"""Preprocess VinDr-Mammo DICOMs -> 8-bit PNGs + build a per-image label CSV for
fine-tuning the Mammo-CLIP B5 classifier on the three structured-finding slots:
mass presence, suspicious-calcification presence, and breast density (A-D).

Recipe (matches the Mammo-CLIP inference preprocessing intent — no crop, so it is
consistent with mammo_clip_adapter._preprocess which just resizes the PIL image):
  DICOM -> apply_voi_lut -> invert if MONOCHROME1 -> robust percentile clip ->
  min-max to [0,255] uint8 -> downscale (longer side <= --max-side, keep aspect) -> PNG

Label CSV columns:
  image_id, path, laterality, view, mass, calc, density_idx, split
  split in {train, val, test}: VinDr's official test split is preserved; a stratified
  val subset is carved out of VinDr's training split (by --val-frac, seeded).

Usage (from repo root, in the mammoclip env):
  python backend/scripts/mammo_finetune/preprocess_vindr.py \
      --vindr-root "dicoms/mamogram/vindr-mammo-...-1.0.0" \
      --out-dir    "backend/scripts/mammo_finetune/data" \
      --workers 8
"""
from __future__ import annotations

import argparse
import ast
import csv
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

DENSITY_MAP = {"DENSITY A": 0, "DENSITY B": 1, "DENSITY C": 2, "DENSITY D": 3}


def _read_dicom_to_uint8(dcm_path: str, max_side: int) -> "np.ndarray | None":
    import pydicom
    from pydicom.pixel_data_handlers.util import apply_voi_lut
    from PIL import Image

    try:
        ds = pydicom.dcmread(dcm_path)
        arr = ds.pixel_array
        # Apply VOI LUT / windowing if present (falls back to raw on failure).
        try:
            arr = apply_voi_lut(arr, ds)
        except Exception:
            pass
        arr = arr.astype(np.float32)
        # MONOCHROME1: higher value == darker -> invert so breast is bright.
        if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
            arr = arr.max() - arr
        # Robust contrast: clip to 1st..99th percentile of non-air pixels.
        lo, hi = np.percentile(arr, (1.0, 99.0))
        if hi <= lo:
            lo, hi = float(arr.min()), float(arr.max())
        arr = np.clip(arr, lo, hi)
        arr = (arr - lo) / (hi - lo + 1e-6)
        arr = (arr * 255.0).astype(np.uint8)

        img = Image.fromarray(arr)
        w, h = img.size
        scale = max_side / float(max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
        return img
    except Exception as e:  # noqa: BLE001
        print(f"[warn] failed {dcm_path}: {e}", file=sys.stderr)
        return None


def _process_one(args_tuple) -> "tuple[str, bool]":
    image_id, dcm_path, png_path, max_side = args_tuple
    if os.path.exists(png_path):
        return image_id, True
    img = _read_dicom_to_uint8(dcm_path, max_side)
    if img is None:
        return image_id, False
    os.makedirs(os.path.dirname(png_path), exist_ok=True)
    img.save(png_path)
    return image_id, True


def build_labels(vindr_root: Path) -> dict:
    """Aggregate finding_annotations.csv -> per-image labels."""
    csv_path = vindr_root / "finding_annotations.csv"
    per_img: dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iid = row["image_id"]
            d = per_img.setdefault(
                iid,
                {
                    "study_id": row["study_id"],
                    "laterality": row["laterality"],
                    "view": row["view_position"],
                    "density": row["breast_density"],
                    "split": row["split"],
                    "mass": 0,
                    "calc": 0,
                    "distortion": 0,
                    "skin_thickening": 0,
                    "nipple_retraction": 0,
                    "lymph_node": 0,
                },
            )
            cats = row["finding_categories"]
            try:
                cats = ast.literal_eval(cats)
            except Exception:
                cats = [cats]
            if "Mass" in cats:
                d["mass"] = 1
            if "Suspicious Calcification" in cats:
                d["calc"] = 1
            if "Architectural Distortion" in cats:
                d["distortion"] = 1
            if "Skin Thickening" in cats:
                d["skin_thickening"] = 1
            # Fold nipple + skin retraction into one "retraction" slot (both rare;
            # the report phrases them together as "retraction seen").
            if "Nipple Retraction" in cats or "Skin Retraction" in cats:
                d["nipple_retraction"] = 1
            if "Suspicious Lymph Node" in cats:
                d["lymph_node"] = 1
    return per_img


def make_val_split(per_img: dict, val_frac: float, seed: int) -> None:
    """Carve a stratified (by density x mass) val split out of VinDr 'training'."""
    rng = np.random.default_rng(seed)
    buckets: dict = defaultdict(list)
    for iid, d in per_img.items():
        if d["split"] != "training":
            continue
        key = (d["density"], d["mass"], d["calc"])
        buckets[key].append(iid)
    for key, ids in buckets.items():
        ids = sorted(ids)
        rng.shuffle(ids)
        n_val = max(1, int(round(len(ids) * val_frac)))
        val_ids = set(ids[:n_val])
        for iid in ids:
            per_img[iid]["split"] = "val" if iid in val_ids else "train"
    # normalise the official test tag
    for d in per_img.values():
        if d["split"] == "test":
            d["split"] = "test"


# Rarity order (rarest first). An image's stratum = the rarest finding it has, so the
# rarest labels are split proportionally across train/val/test (no starved val/test).
_STRATIFY_ORDER = ["nipple_retraction", "lymph_node", "skin_thickening", "distortion", "calc", "mass"]


def assign_kfolds(per_img: dict, k: int, seed: int) -> None:
    """Assign a stratified k-fold index (0..k-1) to every NON-test image; test images get
    fold = -1. Stratified by rarest finding (round-robin within each stratum) so every fold
    has a balanced share of the scarce positives. The official test split stays untouched —
    k-fold runs only over the dev (official training) pool for robust selection + ensembling."""
    rng = np.random.default_rng(seed)
    strata: dict = defaultdict(list)
    for iid, d in per_img.items():
        if d.get("split") == "test":
            d["fold"] = -1
            continue
        stratum = next((lbl for lbl in _STRATIFY_ORDER if d.get(lbl)), None) or f"neg_{d['density']}"
        strata[stratum].append(iid)
    for _stratum, ids in strata.items():
        ids = sorted(ids)
        rng.shuffle(ids)
        for i, iid in enumerate(ids):
            per_img[iid]["fold"] = i % k  # round-robin => balanced fold sizes + rare spread


def make_stratified_split(per_img: dict, val_frac: float, test_frac: float, seed: int) -> None:
    """Custom multi-label split (overrides VinDr's official split). Guarantees each rare
    finding's positives land in train/val/test in the given ratios, and gives training a
    bit more of the scarce positives than the official 80/20 split. NOTE: replacing the
    official split means results are no longer directly comparable to the Mammo-CLIP paper.
    The test set is only re-partitioned here — never augmented or down-sampled."""
    rng = np.random.default_rng(seed)
    strata: dict = defaultdict(list)
    for iid, d in per_img.items():
        stratum = next((lbl for lbl in _STRATIFY_ORDER if d.get(lbl)), None)
        if stratum is None:
            stratum = f"neg_{d['density']}"  # spread pure negatives across density classes
        strata[stratum].append(iid)
    for _stratum, ids in strata.items():
        ids = sorted(ids)
        rng.shuffle(ids)
        n = len(ids)
        n_test = int(round(n * test_frac))
        n_val = int(round(n * val_frac))
        # For small positive strata, guarantee >=1 in each of val/test if possible so the
        # per-label AUROC is at least computable.
        if not _stratum.startswith("neg_") and n >= 3:
            n_test = max(1, n_test)
            n_val = max(1, n_val)
        test_ids = set(ids[:n_test])
        val_ids = set(ids[n_test:n_test + n_val])
        for iid in ids:
            per_img[iid]["split"] = (
                "test" if iid in test_ids else "val" if iid in val_ids else "train"
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vindr-root", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-side", type=int, default=1536)
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--test-frac", type=float, default=0.1, help="only used by --split-mode stratified")
    ap.add_argument("--split-mode", choices=["official", "stratified"], default="official",
                    help="official: VinDr's 16k/4k test + carved val. "
                         "stratified: custom multi-label split maximising rare positives in train.")
    ap.add_argument("--kfolds", type=int, default=0,
                    help="if >0, add a stratified fold index (0..k-1) over the dev pool; "
                         "test images get fold=-1 (used with the trainer's --folds).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--limit", type=int, default=0, help="debug: only process N images")
    ap.add_argument("--labels-only", action="store_true",
                    help="skip DICOM->PNG conversion; only rebuild labels.csv (reuse existing PNGs)")
    args = ap.parse_args()

    vindr_root = Path(args.vindr_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    png_dir = out_dir / "png"
    png_dir.mkdir(parents=True, exist_ok=True)
    images_root = vindr_root / "images"

    print(f"[labels] aggregating findings from {vindr_root/'finding_annotations.csv'}")
    per_img = build_labels(vindr_root)
    if args.split_mode == "stratified":
        make_stratified_split(per_img, args.val_frac, args.test_frac, args.seed)
        print(f"[labels] split-mode=stratified (val={args.val_frac} test={args.test_frac})")
    else:
        make_val_split(per_img, args.val_frac, args.seed)
        print("[labels] split-mode=official (VinDr test split preserved)")
    if args.kfolds > 0:
        assign_kfolds(per_img, args.kfolds, args.seed)
        print(f"[labels] assigned {args.kfolds}-fold index over dev pool (test fold=-1)")
    else:
        for d in per_img.values():
            d["fold"] = -1
    print(f"[labels] {len(per_img)} images")

    # Build work list. VinDr layout: images/<study_id>/<image_id>.dicom
    tasks = []
    label_rows = []
    missing = 0
    for iid, d in per_img.items():
        dcm_path = images_root / d["study_id"] / f"{iid}.dicom"
        if not dcm_path.exists():
            alt = images_root / d["study_id"] / f"{iid}.dcm"
            dcm_path = alt if alt.exists() else dcm_path
        png_path = png_dir / d["study_id"] / f"{iid}.png"
        if not dcm_path.exists():
            missing += 1
            continue
        tasks.append((iid, str(dcm_path), str(png_path), args.max_side))
        if d["density"] not in DENSITY_MAP:
            continue
        label_rows.append(
            {
                "image_id": iid,
                "path": str(png_path.relative_to(out_dir)),
                "laterality": d["laterality"],
                "view": d["view"],
                "mass": d["mass"],
                "calc": d["calc"],
                "distortion": d["distortion"],
                "skin_thickening": d["skin_thickening"],
                "nipple_retraction": d["nipple_retraction"],
                "lymph_node": d["lymph_node"],
                "density_idx": DENSITY_MAP[d["density"]],
                "split": d["split"],
                "fold": d.get("fold", -1),
            }
        )
    if missing:
        print(f"[warn] {missing} images missing on disk")
    if args.limit:
        tasks = tasks[: args.limit]

    if args.labels_only:
        print("[png] --labels-only: skipping DICOM->PNG conversion, reusing existing PNGs")
    else:
        print(f"[png] converting {len(tasks)} DICOMs with {args.workers} workers ...")
        ok = 0
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(_process_one, t) for t in tasks]
            for i, fut in enumerate(as_completed(futs), 1):
                _, good = fut.result()
                ok += int(good)
                if i % 500 == 0:
                    print(f"[png] {i}/{len(tasks)} done ({ok} ok)")
        print(f"[png] finished: {ok}/{len(tasks)} converted")

    # Only keep label rows whose PNG actually exists.
    labels_csv = out_dir / "labels.csv"
    kept = 0
    with open(labels_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "image_id", "path", "laterality", "view",
                "mass", "calc", "distortion", "skin_thickening",
                "nipple_retraction", "lymph_node", "density_idx", "split", "fold",
            ],
        )
        w.writeheader()
        for r in label_rows:
            if (out_dir / r["path"]).exists():
                w.writerow(r)
                kept += 1
    print(f"[labels] wrote {kept} rows -> {labels_csv}")

    from collections import Counter

    by_split = Counter(r["split"] for r in label_rows if (out_dir / r["path"]).exists())
    print(f"[labels] split sizes: {dict(by_split)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
