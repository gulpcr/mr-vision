#!/usr/bin/env python3
"""Fine-tune the released Mammo-CLIP B5 image encoder into a supervised MULTI-LABEL
classifier for every structured finding the mammography report narrates:

  binary presence: mass, calc (suspicious calcification), distortion
                   (architectural distortion), skin_thickening,
                   nipple_retraction (nipple OR skin retraction), lymph_node
                   (suspicious axillary lymph node)
  4-way:           density (A/B/C/D)

Validation design (chosen by the user):
  * The official VinDr 4k test split is held out untouched (comparable to the paper),
    evaluated ONCE at the end by the ENSEMBLE of the fold models.
  * k-fold cross-validation runs over the 16k dev pool (fold column from preprocess):
    fold f -> val, the rest -> train. Robust selection + a k-model ensemble.
  * Rare-label scarcity is handled on the TRAIN side only: class-weighted losses +
    a WeightedRandomSampler that oversamples rare-finding images (default 2x). The test
    set is NEVER augmented or down-sampled.
  * Rare-label test AUROCs are reported with 95% bootstrap confidence intervals so their
    (un)reliability is explicit.

Run (repo root, mammoclip env):
  python backend/scripts/mammo_finetune/train_vindr_classifier.py \
      --data-dir backend/scripts/mammo_finetune/data \
      --ckpt     C:/model_cache/mammo_clip/b5-model-best-epoch-7.tar \
      --out-dir  backend/scripts/mammo_finetune/runs/b5_vindr_kfold \
      --folds 5 --epochs 12 --batch-size 2 --grad-accum 8 --oversample-factor 2.0
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

EXT_DIR = Path(__file__).resolve().parents[2] / "external" / "Mammo-CLIP"

BINARY_LABELS = ["mass", "calc", "distortion", "skin_thickening", "nipple_retraction", "lymph_node"]
# Scarce findings that the sampler oversamples on the train side.
RARE_LABELS = ["distortion", "skin_thickening", "nipple_retraction", "lymph_node"]
# Labels whose val set has enough positives to trust for model selection / early-stop.
SELECTION_LABELS = ["mass", "calc", "density"]

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


class VinDrDataset(Dataset):
    def __init__(self, rows, data_dir, size_h, size_w, mean, std, train=False):
        self.rows = rows
        self.data_dir = Path(data_dir)
        self.size_h = size_h  # PIL resize width  (matches adapter: resize((size_h, size_w)))
        self.size_w = size_w  # PIL resize height
        self.mean = mean
        self.std = std
        if train:
            from torchvision import transforms

            self.aug = transforms.Compose(
                [
                    transforms.RandomHorizontalFlip(0.5),
                    transforms.RandomAffine(degrees=8, translate=(0.04, 0.04), scale=(0.92, 1.08)),
                    transforms.ColorJitter(brightness=0.10, contrast=0.10),
                ]
            )
        else:
            self.aug = None

    def __len__(self):
        return len(self.rows)

    def _load(self, path) -> torch.Tensor:
        img = Image.open(self.data_dir / path).convert("RGB")
        img = img.resize((self.size_h, self.size_w))  # -> (W=size_h, H=size_w)
        if self.aug is not None:
            img = self.aug(img)
        arr = np.asarray(img).astype("float32")
        arr -= arr.min()
        mx = float(arr.max())
        if mx > 0:
            arr /= mx
        arr = (arr - self.mean) / self.std
        return torch.from_numpy(arr).permute(2, 0, 1)  # (C, H, W)

    def __getitem__(self, i):
        r = self.rows[i]
        x = self._load(r["path"])
        y = {lbl: torch.tensor(float(r[lbl])) for lbl in BINARY_LABELS}
        y["density"] = torch.tensor(int(r["density_idx"]))
        return x, y


def read_labels(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for lbl in BINARY_LABELS:
                r[lbl] = int(r[lbl])
            r["density_idx"] = int(r["density_idx"])
            r["fold"] = int(r.get("fold", -1))
            rows.append(r)
    return rows


def build_sampler(train_rows, factor):
    """WeightedRandomSampler that draws rare-finding images `factor`x more often. The
    factor is set by us, not auto-tuned — 2x means a rare-containing image is ~twice as
    likely to be drawn as a pure/common one. num_samples == len(train) so epoch length is
    unchanged; rare positives simply recur more within the epoch."""
    if factor is None or factor <= 1.0:
        return None
    weights = [float(factor) if any(r[l] for l in RARE_LABELS) else 1.0 for r in train_rows]
    return WeightedRandomSampler(weights, num_samples=len(train_rows), replacement=True)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class MammoClipFinetune(nn.Module):
    """Released B5 image encoder + one linear head per finding (binary) + density (4-way)."""

    def __init__(self, ckpt_path, bn_eval=True, dropout=0.1):
        super().__init__()
        sys.path.insert(0, str(EXT_DIR))
        from breastclip.model.modules import load_image_encoder

        ckpt = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
        cfg = ckpt["config"]
        enc_cfg = copy.deepcopy(cfg["model"]["image_encoder"])
        enc_cfg["pretrained"] = False
        if "cache_dir" in enc_cfg:
            enc_cfg["cache_dir"] = str(Path(ckpt_path).parent / "hf_cache")
        self.encoder = load_image_encoder(enc_cfg)

        enc_w = {
            k[len("image_encoder.") :]: v
            for k, v in ckpt["model"].items()
            if k.startswith("image_encoder.")
        }
        missing, unexpected = self.encoder.load_state_dict(enc_w, strict=False)
        matched = len(enc_w) - len(unexpected)
        print(
            f"[model] loaded encoder weights: matched={matched}/{len(enc_w)} "
            f"missing={len(missing)} unexpected={len(unexpected)}"
        )
        if matched < 0.9 * len(enc_w):
            raise RuntimeError("Encoder weight load matched <90% of keys — config mismatch?")

        self.out_dim = int(self.encoder.out_dim)
        self.bn_eval = bn_eval
        self.drop = nn.Dropout(dropout)
        self.binary_heads = nn.ModuleDict({lbl: nn.Linear(self.out_dim, 1) for lbl in BINARY_LABELS})
        self.head_density = nn.Linear(self.out_dim, 4)

    def set_encoder_trainable(self, flag: bool):
        for p in self.encoder.parameters():
            p.requires_grad = flag

    def train(self, mode: bool = True):
        super().train(mode)
        if mode and self.bn_eval:
            self.encoder.eval()  # freeze backbone BN stats (upstream recipe)
        return self

    def forward(self, images):
        feat = self.encoder(images)
        if isinstance(feat, (tuple, list)):
            feat = feat[0]
        feat = self.drop(feat)
        out = {lbl: self.binary_heads[lbl](feat).squeeze(1) for lbl in BINARY_LABELS}
        out["density"] = self.head_density(feat)
        return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_class_weights(train_rows, pos_weight_cap):
    n = len(train_rows)
    pos_w = {}
    for lbl in BINARY_LABELS:
        n_pos = sum(r[lbl] for r in train_rows)
        pos_w[lbl] = float(min((n - n_pos) / max(1, n_pos), pos_weight_cap))
    dens_counts = np.zeros(4)
    for r in train_rows:
        dens_counts[r["density_idx"]] += 1
    dens_w = n / (4.0 * np.clip(dens_counts, 1, None))
    dens_w = dens_w / dens_w.mean()
    return pos_w, torch.tensor(dens_w, dtype=torch.float32)


@torch.no_grad()
def predict(model, loader, device):
    """Return per-label predicted probabilities + targets (raw, for ensembling/CIs)."""
    model.eval()
    P = {lbl: [] for lbl in BINARY_LABELS}
    T = {lbl: [] for lbl in BINARY_LABELS}
    D, DT = [], []
    for x, y in loader:
        x = x.to(device, non_blocking=True)
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            out = model(x)
        for lbl in BINARY_LABELS:
            P[lbl].append(torch.sigmoid(out[lbl]).float().cpu().numpy())
            T[lbl].append(y[lbl].numpy())
        D.append(torch.softmax(out["density"].float(), dim=1).cpu().numpy())
        DT.append(y["density"].numpy())
    P = {lbl: np.concatenate(P[lbl]) for lbl in BINARY_LABELS}
    T = {lbl: np.concatenate(T[lbl]) for lbl in BINARY_LABELS}
    return P, np.concatenate(D), T, np.concatenate(DT)


def _metrics_from_preds(P, D, T, DT):
    from sklearn.metrics import roc_auc_score

    res = {}
    for lbl in BINARY_LABELS:
        t = T[lbl]
        res[f"{lbl}_pos"] = int(t.sum())
        res[f"{lbl}_auroc"] = float(roc_auc_score(t, P[lbl])) if len(set(t.tolist())) > 1 else float("nan")
    try:
        res["density_auroc"] = float(
            roc_auc_score(DT, D, multi_class="ovr", average="macro", labels=list(range(4)))
        )
    except Exception:
        res["density_auroc"] = float("nan")
    res["density_acc"] = float((D.argmax(1) == DT).mean())
    sel = [res["density_auroc"] if k == "density" else res[f"{k}_auroc"] for k in SELECTION_LABELS]
    sel = [v for v in sel if not np.isnan(v)]
    res["selection_auroc"] = float(np.mean(sel)) if sel else float("nan")
    alla = [res[f"{lbl}_auroc"] for lbl in BINARY_LABELS] + [res["density_auroc"]]
    alla = [v for v in alla if not np.isnan(v)]
    res["mean_auroc"] = float(np.mean(alla)) if alla else float("nan")
    return res


def evaluate(model, loader, device):
    P, D, T, DT = predict(model, loader, device)
    return _metrics_from_preds(P, D, T, DT)


def auroc_ci(t, p, n_boot=2000, seed=0):
    from sklearn.metrics import roc_auc_score

    t = np.asarray(t); p = np.asarray(p)
    if len(set(t.tolist())) < 2:
        return float("nan"), float("nan"), float("nan")
    base = roc_auc_score(t, p)
    rng = np.random.default_rng(seed)
    n = len(t)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        tt = t[idx]
        if len(set(tt.tolist())) < 2:
            continue
        vals.append(roc_auc_score(tt, p[idx]))
    if not vals:
        return float(base), float("nan"), float("nan")
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return float(base), float(lo), float(hi)


def _fmt(res):
    parts = [f"{lbl}={res[f'{lbl}_auroc']:.3f}(n{res[f'{lbl}_pos']})" for lbl in BINARY_LABELS]
    parts.append(f"density={res['density_auroc']:.3f}/acc{res['density_acc']:.3f}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Training (one fold / one split)
# ---------------------------------------------------------------------------


def run_training(train_rows, val_rows, args, geom, out_path, tag, fixed_epochs=None):
    """Train one model. If val_rows is empty AND fixed_epochs is set, run in
    "final-on-all" mode: no validation / early-stopping, train fixed_epochs on all data
    and save the last model (used to fit the single shippable model on all dev data for
    the CV-median epoch count)."""
    device = "cuda"
    fixed_mode = not val_rows and fixed_epochs is not None
    n_epochs = fixed_epochs if fixed_mode else args.epochs
    size_h, size_w, mean, std = geom
    ds_tr = VinDrDataset(train_rows, args.data_dir, size_h, size_w, mean, std, train=True)
    sampler = build_sampler(train_rows, args.oversample_factor)
    dl_tr = DataLoader(ds_tr, batch_size=args.batch_size, sampler=sampler,
                       shuffle=(sampler is None), num_workers=args.workers, pin_memory=True,
                       drop_last=True, persistent_workers=args.workers > 0)
    dl_va = None
    if not fixed_mode:
        ds_va = VinDrDataset(val_rows, args.data_dir, size_h, size_w, mean, std, train=False)
        dl_va = DataLoader(ds_va, batch_size=args.batch_size, shuffle=False, num_workers=args.workers,
                           pin_memory=True, persistent_workers=args.workers > 0)

    pos_w, dens_w = compute_class_weights(train_rows, args.pos_weight_cap)
    print(f"[{tag}] pos_weight(cap{args.pos_weight_cap:.0f})="
          + ",".join(f"{k}={v:.1f}" for k, v in pos_w.items())
          + f" | oversample={args.oversample_factor}x rare")

    model = MammoClipFinetune(args.ckpt, bn_eval=not args.no_bn_eval).to(device)
    losses = {lbl: nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_w[lbl], device=device))
              for lbl in BINARY_LABELS}
    loss_dens = nn.CrossEntropyLoss(weight=dens_w.to(device))

    head_params = list(model.binary_heads.parameters()) + list(model.head_density.parameters())
    opt = torch.optim.AdamW(
        [{"params": head_params, "lr": args.lr_head},
         {"params": list(model.encoder.parameters()), "lr": args.lr_encoder}],
        weight_decay=args.weight_decay,
    )
    steps_per_epoch = max(1, len(dl_tr) // args.grad_accum)
    total_steps = steps_per_epoch * n_epochs
    warmup_steps = int(total_steps * args.warmup_frac)

    def lr_lambda(step):
        if step < warmup_steps:
            return step / max(1, warmup_steps)
        prog = (step - warmup_steps) / max(1, total_steps - warmup_steps)
        return 0.5 * (1 + np.cos(np.pi * prog))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda)
    scaler = torch.amp.GradScaler("cuda")

    best_sel, best_epoch, since = -1.0, -1, 0
    for epoch in range(n_epochs):
        model.set_encoder_trainable(epoch >= args.freeze_epochs)
        phase = "heads" if epoch < args.freeze_epochs else "full"
        model.train()
        t0 = time.time()
        running = 0.0
        opt.zero_grad(set_to_none=True)
        for it, (x, y) in enumerate(dl_tr):
            x = x.to(device, non_blocking=True)
            yb = {lbl: y[lbl].to(device) for lbl in BINARY_LABELS}
            yd = y["density"].to(device)
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                out = model(x)
                l = sum(losses[lbl](out[lbl], yb[lbl]) for lbl in BINARY_LABELS)
                l = (l + loss_dens(out["density"], yd)) / args.grad_accum
            scaler.scale(l).backward()
            running += l.detach().item() * args.grad_accum
            if (it + 1) % args.grad_accum == 0:
                scaler.unscale_(opt)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                prev = scaler.get_scale()
                scaler.step(opt)
                scaler.update()
                opt.zero_grad(set_to_none=True)
                if scaler.get_scale() >= prev:
                    sched.step()

        save_dict = {"model": model.state_dict(), "epoch": epoch,
                     "binary_labels": BINARY_LABELS,
                     "cfg": {"size_h": size_h, "size_w": size_w, "mean": mean, "std": std,
                             "out_dim": model.out_dim, "ckpt": str(args.ckpt)}}
        if fixed_mode:
            # No val: save every epoch so the last one is the final model.
            save_dict["val"] = None
            torch.save(save_dict, out_path)
            print(f"[{tag} e{epoch}] ({phase}) loss={running/max(1,len(dl_tr)):.3f} "
                  f"(fixed {n_epochs}ep, saved) {time.time()-t0:.0f}s")
            continue

        val = evaluate(model, dl_va, device)
        print(f"[{tag} e{epoch}] ({phase}) loss={running/max(1,len(dl_tr)):.3f} "
              f"sel={val['selection_auroc']:.4f} | {_fmt(val)} {time.time()-t0:.0f}s")
        if val["selection_auroc"] > best_sel:
            best_sel, best_epoch, since = val["selection_auroc"], epoch, 0
            save_dict["val"] = val
            torch.save(save_dict, out_path)
            print(f"  -> saved {out_path.name} (sel={best_sel:.4f})")
        else:
            since += 1
            if since >= args.patience:
                print(f"[{tag}] early-stop (no val gain {args.patience} epochs)")
                break
    del model
    torch.cuda.empty_cache()
    return {"tag": tag, "best_epoch": best_epoch, "best_sel": best_sel}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--folds", type=int, default=0, help="k-fold CV over dev pool; 0 = single split")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--lr-head", type=float, default=3e-4)
    ap.add_argument("--lr-encoder", type=float, default=1e-5)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--freeze-epochs", type=int, default=1)
    ap.add_argument("--warmup-frac", type=float, default=0.05)
    ap.add_argument("--pos-weight-cap", type=float, default=50.0)
    ap.add_argument("--oversample-factor", type=float, default=2.0,
                    help="WeightedRandomSampler multiplier for rare-finding images (1.0 = off)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--patience", type=int, default=3)
    ap.add_argument("--size-h", type=int, default=0)
    ap.add_argument("--size-w", type=int, default=0)
    ap.add_argument("--no-bn-eval", action="store_true")
    ap.add_argument("--final-on-all", action="store_true", default=True,
                    help="after k-fold CV, retrain ONE model on all dev data (CV-median epochs) to ship")
    ap.add_argument("--no-final-on-all", dest="final_on_all", action="store_false")
    ap.add_argument("--limit", type=int, default=0, help="debug: cap dev+test rows for a smoke test")
    args = ap.parse_args()

    if not torch.cuda.is_available():
        print("[fatal] CUDA not available — refusing to train on CPU.")
        return 1
    torch.backends.cudnn.benchmark = True
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ckpt = torch.load(str(args.ckpt), map_location="cpu", weights_only=False)
    base = ckpt["config"].get("base", {})
    rs = ckpt["config"].get("transform", {}).get("test", {}).get("Resize", {})
    size_h = args.size_h or int(rs.get("size_h", base.get("image_size_h", 1520)))
    size_w = args.size_w or int(rs.get("size_w", base.get("image_size_w", 912)))
    mean = float(base.get("mean", 0.3089279))
    std = float(base.get("std", 0.25053555408335154))
    del ckpt
    geom = (size_h, size_w, mean, std)
    print(f"[cfg] resize=({size_h},{size_w}) mean={mean:.4f} std={std:.4f} folds={args.folds}")

    rows = read_labels(Path(args.data_dir) / "labels.csv")
    test_rows = [r for r in rows if r["split"] == "test"]
    dev_rows = [r for r in rows if r["fold"] >= 0] or [r for r in rows if r["split"] != "test"]
    if args.limit:
        dev_rows = dev_rows[: args.limit]
        test_rows = test_rows[: args.limit]
    print(f"[data] dev={len(dev_rows)} test={len(test_rows)}")

    # -- train (k-fold or single split), resumable: skip folds already saved --
    fold_ckpts = []
    if args.folds and args.folds > 1:
        for f in range(args.folds):
            out_path = out_dir / f"best_fold{f}.pt"
            fold_ckpts.append(out_path)
            if out_path.exists():
                print(f"[fold{f}] exists -> skip (resume)")
                continue
            tr = [r for r in dev_rows if r["fold"] != f]
            va = [r for r in dev_rows if r["fold"] == f]
            print(f"[fold{f}] train={len(tr)} val={len(va)}")
            run_training(tr, va, args, geom, out_path, tag=f"fold{f}")
    else:
        out_path = out_dir / "best.pt"
        fold_ckpts.append(out_path)
        tr = [r for r in dev_rows if r["split"] == "train"] or dev_rows
        va = [r for r in dev_rows if r["split"] == "val"] or dev_rows[: max(1, len(dev_rows) // 10)]
        if not out_path.exists():
            run_training(tr, va, args, geom, out_path, tag="single")

    # -- CV summary from saved fold val metrics --
    cv = []
    for p in fold_ckpts:
        if p.exists():
            cv.append(torch.load(p, map_location="cpu")["val"]["selection_auroc"])
    if len(cv) > 1:
        print(f"[CV] selection_auroc across folds: mean={np.mean(cv):.4f} std={np.std(cv):.4f} {['%.3f'%c for c in cv]}")

    if not test_rows:
        print("[warn] no test rows; skipping final eval")
        return 0
    ds_te = VinDrDataset(test_rows, args.data_dir, size_h, size_w, mean, std, train=False)
    dl_te = DataLoader(ds_te, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)

    def eval_ckpts(paths, tag):
        """Average predictions over `paths` (1 => single model, k => ensemble) on the
        fixed test set; return metrics + per-label bootstrap CIs."""
        paths = [p for p in paths if p.exists()]
        sumP = {lbl: None for lbl in BINARY_LABELS}
        sumD, T, DT = None, None, None
        for p in paths:
            m = MammoClipFinetune(args.ckpt, bn_eval=not args.no_bn_eval).to("cuda")
            m.load_state_dict(torch.load(p, map_location="cuda")["model"])
            P, D, T, DT = predict(m, dl_te, "cuda")
            for lbl in BINARY_LABELS:
                sumP[lbl] = P[lbl] if sumP[lbl] is None else sumP[lbl] + P[lbl]
            sumD = D if sumD is None else sumD + D
            del m
            torch.cuda.empty_cache()
        nk = len(paths)
        avgP = {lbl: sumP[lbl] / nk for lbl in BINARY_LABELS}
        avgD = sumD / nk
        res = _metrics_from_preds(avgP, avgD, T, DT)
        res["ci95"] = {}
        for lbl in BINARY_LABELS:
            b, lo, hi = auroc_ci(T[lbl], avgP[lbl], seed=0)
            res["ci95"][lbl] = {"auroc": b, "ci95": [lo, hi], "pos": int(T[lbl].sum())}
        res["n_models"] = nk
        print(f"\n[TEST {tag} ({nk} model{'s' if nk>1 else ''})] "
              f"sel={res['selection_auroc']:.4f} mean={res['mean_auroc']:.4f}")
        for lbl in BINARY_LABELS:
            c = res["ci95"][lbl]
            print(f"  {lbl:18s} AUROC={c['auroc']:.3f}  95% CI [{c['ci95'][0]:.3f}, {c['ci95'][1]:.3f}]  (pos={c['pos']})")
        print(f"  {'density':18s} AUROC={res['density_auroc']:.3f}  acc={res['density_acc']:.3f}")
        return res

    out = {}
    if args.folds and args.folds > 1:
        out["ensemble"] = eval_ckpts(fold_ckpts, "ensemble")
        # Final single model on ALL dev, trained for the CV-median best-epoch count.
        if args.final_on_all:
            best_epochs = [torch.load(p, map_location="cpu")["epoch"]
                           for p in fold_ckpts if p.exists()]
            n_ep = max(args.freeze_epochs + 1, int(round(float(np.mean(best_epochs)))) + 1)
            final_path = out_dir / "best_all.pt"
            if not final_path.exists():
                print(f"\n[final] retrain on all dev ({len(dev_rows)} imgs) for {n_ep} epochs")
                run_training(dev_rows, [], args, geom, final_path, tag="final", fixed_epochs=n_ep)
            out["single"] = eval_ckpts([final_path], "single(all-dev)")
    else:
        out["single"] = eval_ckpts(fold_ckpts, "single")

    with open(out_dir / "test_metrics.json", "w") as f:
        json.dump(out, f, indent=2)
    if "ensemble" in out and "single" in out:
        d = out["ensemble"]["mean_auroc"] - out["single"]["mean_auroc"]
        print(f"\n[compare] ensemble mean AUROC {out['ensemble']['mean_auroc']:.4f} vs "
              f"single {out['single']['mean_auroc']:.4f} (delta={d:+.4f}) -- "
              f"ship single unless delta is materially positive on labels that matter.")
    print(f"[done] wrote {out_dir/'test_metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
