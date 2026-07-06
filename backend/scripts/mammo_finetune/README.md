# Mammo-CLIP B5 fine-tune on VinDr-Mammo

Fine-tunes the **released Mammo-CLIP B5 image encoder** into a supervised multi-task
classifier for the three structured-finding slots the mammography pipeline uses:

| Slot | Task | VinDr source |
|------|------|--------------|
| `mass` | binary presence | `finding_categories` contains `Mass` |
| `calc` | binary presence | `finding_categories` contains `Suspicious Calcification` |
| `density` | 4-way (A/B/C/D) | `breast_density` |

## Why fine-tune (not pretrain)

Mammo-CLIP's *pretraining* is contrastive **image↔report**. VinDr-Mammo has labels but
**no free-text reports**, so CLIP pretraining is not possible on it — and training a
foundation model from scratch on one 20k set would underperform the released checkpoint.
Fine-tuning the released B5 backbone on VinDr labels is the accuracy-maximising option and
directly upgrades the pipeline's current *zero-shot* slot-filling to a trained head.

## Metric: AUROC, not accuracy

Prevalence is extreme (mass ~5.6%, calc ~2.2%; density is 76% C / 0.5% A). A constant
"negative" predictor scores ~98% accuracy but is useless, so we optimise/report **AUROC**
per label (matching the Mammo-CLIP paper's VinDr protocol). Losses are class-weighted
(BCE `pos_weight` for mass/calc, weighted CE for density).

## Environment

Conda env `mammoclip` (Python 3.11) with Blackwell-compatible CUDA torch:

```bash
conda create -y -n mammoclip python=3.11
conda run -n mammoclip python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
conda run -n mammoclip python -m pip install numpy pillow scikit-learn pandas tqdm timm transformers \
    pydicom pylibjpeg pylibjpeg-openjpeg pylibjpeg-libjpeg omegaconf huggingface_hub
```

Verified on 2× RTX 5070 Ti (Blackwell sm_120), torch 2.11.0+cu128.

> Call the env python directly (`C:/Users/<you>/.conda/envs/mammoclip/python.exe`) — `conda run`
> can't pass multi-line `-c` scripts on Windows.

## 1. Stage the released checkpoint

```bash
python backend/scripts/download_mammo_clip.py
# -> C:\model_cache\mammo_clip\b5-model-best-epoch-7.tar   (1654 MB; /model_cache is drive-root on Windows)
```

## 2. Preprocess DICOM → PNG + build labels

```bash
python backend/scripts/mammo_finetune/preprocess_vindr.py \
  --vindr-root "dicoms/mamogram/vindr-mammo-...-1.0.0" \
  --out-dir    backend/scripts/mammo_finetune/data \
  --workers 16
```

Produces `data/png/<study>/<image>.png` (8-bit, VOI-LUT + MONOCHROME1 handled, percentile
clip, longer side ≤ 1536) and `data/labels.csv` (official VinDr test split preserved; a
stratified 10% val split carved out of training). Re-runnable: existing PNGs are skipped.

## 3. Fine-tune

```bash
python backend/scripts/mammo_finetune/train_vindr_classifier.py \
  --data-dir backend/scripts/mammo_finetune/data \
  --ckpt     C:/model_cache/mammo_clip/b5-model-best-epoch-7.tar \
  --out-dir  backend/scripts/mammo_finetune/runs/b5_vindr \
  --epochs 15 --batch-size 8 --grad-accum 2
```

- Image geometry + normalisation are read from the checkpoint (resize 1520×912, per-image
  min-max, mean/std) so training matches `mammo_clip_adapter._preprocess` exactly.
- Backbone BatchNorm kept in eval mode during fine-tune (upstream recipe; small high-res
  batches make BN stats noisy). First `--freeze-epochs` train heads only, then unfreeze.
- Best checkpoint (by val mean-AUROC) → `runs/b5_vindr/best.pt`; final held-out test metrics
  → `runs/b5_vindr/test_metrics.json`.
- `--limit N` caps rows per split for a fast smoke test.

## Output checkpoint

`best.pt` = `{model: state_dict, cfg: {size_h,size_w,mean,std,out_dim,ckpt}, val: {...}}`.
The `model` state dict is a `MammoClipFinetune` (B5 encoder + `head_mass`/`head_calc`/`head_density`).

## License

Mammo-CLIP is **CC BY-NC-SA 4.0 (non-commercial)**. Anything fine-tuned from it inherits that
restriction — research/prototype only. For a commercial deployment, replace with a
commercially-licensed backbone (e.g. `ianpan/mammoscreen`, Apache-2.0) and re-run this pipeline.
