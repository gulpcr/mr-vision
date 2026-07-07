"""Inference adapter for the VinDr-Mammo fine-tuned Mammo-CLIP B5 multi-label classifier.

Rebuilds the exact architecture trained in backend/scripts/mammo_finetune/train_vindr_classifier.py
(released B5 image encoder + one linear head per finding + a 4-way density head) and loads the
shipped `best_all.pt` weights. Exposes `predict(pil_image) -> {label: prob}`:

  binary findings (sigmoid): mass, calc, distortion, skin_thickening, nipple_retraction, lymph_node
  density (softmax over a,b,c,d): "density" -> [p_a, p_b, p_c, p_d]

Preprocessing reproduces training/inference exactly (from the checkpoint cfg): RGB -> resize
(size_h x size_w) -> per-image min-max to [0,1] -> (x-mean)/std -> CHW.

Rebuilding the encoder needs the RELEASED B5 checkpoint (for its image_encoder architecture config)
— pass `released_ckpt_path`; falls back to the path stored in best_all's cfg.

Vendored from batmanlab/Mammo-CLIP — CC BY-NC-SA 4.0 (non-commercial).
"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

BINARY_LABELS = ["mass", "calc", "distortion", "skin_thickening", "nipple_retraction", "lymph_node"]


class _MammoClipFinetune(nn.Module):
    """Released B5 image encoder + per-finding linear heads (must match the training class)."""

    def __init__(self, encoder, out_dim, dropout=0.1):
        super().__init__()
        self.encoder = encoder
        self.out_dim = out_dim
        self.drop = nn.Dropout(dropout)
        self.binary_heads = nn.ModuleDict({lbl: nn.Linear(out_dim, 1) for lbl in BINARY_LABELS})
        self.head_density = nn.Linear(out_dim, 4)

    def forward(self, images):
        feat = self.encoder(images)
        if isinstance(feat, (tuple, list)):
            feat = feat[0]
        feat = self.drop(feat)
        out = {lbl: self.binary_heads[lbl](feat).squeeze(1) for lbl in BINARY_LABELS}
        out["density"] = self.head_density(feat)
        return out


class MammoClipFinetunedClassifier:
    def __init__(self, model, device, *, size_h, size_w, mean, std):
        self.model = model
        self.device = device
        self.size_h = size_h
        self.size_w = size_w
        self.mean = mean
        self.std = std

    def _preprocess(self, pil_image) -> torch.Tensor:
        img = pil_image.convert("RGB").resize((self.size_h, self.size_w))
        arr = np.asarray(img).astype("float32")
        arr -= arr.min()
        mx = float(arr.max())
        if mx > 0:
            arr /= mx
        arr = (arr - self.mean) / self.std
        t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
        return t.to(self.device)

    @torch.no_grad()
    def predict(self, pil_image) -> dict:
        """Per-image finding probabilities. Binary labels -> float in [0,1];
        'density' -> list of 4 softmax probabilities [a, b, c, d]."""
        x = self._preprocess(pil_image)
        if self.device.type == "cuda":
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                out = self.model(x)
        else:
            out = self.model(x)
        res = {lbl: float(torch.sigmoid(out[lbl]).float().item()) for lbl in BINARY_LABELS}
        res["density"] = [float(p) for p in torch.softmax(out["density"].float(), dim=1)[0].cpu().numpy()]
        return res


def load_model(ckpt_path: str, device, released_ckpt_path: str | None = None) -> MammoClipFinetunedClassifier:
    """Load the fine-tuned classifier. `ckpt_path` is best_all.pt; `released_ckpt_path` is the
    released B5 .tar needed to rebuild the encoder architecture (defaults to the path recorded
    in best_all's cfg)."""
    from breastclip.model.modules import load_image_encoder

    state = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
    cfg = state["cfg"]
    released = released_ckpt_path or cfg.get("ckpt")
    if not released or not Path(released).exists():
        raise FileNotFoundError(
            f"Released B5 checkpoint not found ({released}) — needed to rebuild the encoder."
        )

    rel = torch.load(str(released), map_location="cpu", weights_only=False)
    enc_cfg = copy.deepcopy(rel["config"]["model"]["image_encoder"])
    enc_cfg["pretrained"] = False
    if "cache_dir" in enc_cfg:
        enc_cfg["cache_dir"] = str(Path(released).parent / "hf_cache")
    encoder = load_image_encoder(enc_cfg)

    model = _MammoClipFinetune(encoder, int(cfg["out_dim"]))
    model.load_state_dict(state["model"], strict=True)
    model.eval().to(device)

    return MammoClipFinetunedClassifier(
        model, device,
        size_h=int(cfg["size_h"]), size_w=int(cfg["size_w"]),
        mean=float(cfg["mean"]), std=float(cfg["std"]),
    )
