# Mammo-CLIP (vendored) — zero-shot structured mammography findings

This directory holds the vendored [batmanlab/Mammo-CLIP](https://github.com/batmanlab/Mammo-CLIP)
inference code. The mammography pipeline uses it for **zero-shot** classification of the structured
finding slots — breast **density (a–d)**, **mass** presence, and **calcification** presence — with
no training required.

> ⚠️ **License:** Mammo-CLIP is released under **CC BY-NC-SA 4.0 (non-commercial)**. It is enabled
> here for a research/prototype deployment only. Do **not** enable it in a commercial product
> without replacing it with a commercially-licensed model (e.g. `ianpan/mammoscreen`, Apache-2.0).

## How it is wired

- Gated by `Settings.mammography_clip_enabled` (env `MAMMOGRAPHY_CLIP_ENABLED`), off by default.
- Weights live at `Settings.mammography_clip_weights_path` (default `/model_cache/mammo_clip`),
  staged into the `model_cache` Docker volume.
- Prompts / thresholds are in `app/usecases/mammography/model/inference_config.yaml` (`mammo_clip:`).
- The pipeline loads it in `MammographyPipeline._load_mammo_clip()` and runs it in
  `_run_mammo_clip()`. **Any** failure (code missing, weights missing, deps missing, API mismatch)
  degrades gracefully: the model-fillable slots are simply left unset for the radiologist and a
  `mammo_clip_unavailable` QA flag is raised. It never breaks the pipeline.

## Setup (run in the GPU/worker environment)

```bash
# 1. Vendor the code + download the B5 checkpoint into the model_cache volume
docker compose exec worker python scripts/download_mammo_clip.py

# 2. Enable it
#    In .env:  MAMMOGRAPHY_CLIP_ENABLED=true
docker compose restart worker
```

## What's vendored here (already finalized)

- `breastclip/` — the upstream `breastclip` package (model subtree), copied from
  batmanlab/Mammo-CLIP. Two intentional edits for inference-only use:
  - `breastclip/__init__.py` — minimized (upstream eagerly imported trainer/validator →
    hydra/albumentations); import the model directly via `from breastclip.model import build_model`.
  - `breastclip/model/modules/__init__.py` — the two `-detect` EfficientNet branches use
    `EfficientNet.from_name(...)` instead of `from_pretrained(...)` (no ImageNet download; the
    Mammo-CLIP checkpoint state_dict is loaded strict=False afterward and supplies all weights).
- `mammo_clip_adapter.py` — `load_model(ckpt_path, device) -> MammoClipZeroShot`, exposing
  `zero_shot(pil_image, prompts) -> list[float]`. Reproduces the upstream zero-shot recipe faithfully
  (RGB → resize 1520×912 → per-image min-max → `(x-mean)/std`; text via Bio_ClinicalBERT eos-pooling;
  softmax over cosine similarity). Builds encoders with `pretrained=False` and loads the checkpoint
  state_dict (verified: 1056/1056 model params match).

The mammography pipeline (`_load_mammo_clip`) puts this directory on `sys.path` and calls the adapter.
The B5 checkpoint (`b5-model-best-epoch-7.tar`) is staged into the weights dir by
`scripts/download_mammo_clip.py`. No further manual step is required.
