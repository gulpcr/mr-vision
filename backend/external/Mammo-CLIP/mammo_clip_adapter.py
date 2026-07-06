"""Thin inference adapter over the vendored Mammo-CLIP `breastclip` package.

Reproduces the upstream zero-shot recipe faithfully (see breastclip/evaluator.py
`eval_zeroshot` + breastclip/data/datasets/image_classification_zs.py) so results
match the released model:

  image:  RGB -> resize (size_h x size_w) -> per-image min-max to [0,1] ->
          (x - mean) / std -> CHW tensor
  encode: model.encode_image -> image_projection (if projection) -> L2-normalize
  text:   tokenizer(prompts, max_length) -> model.encode_text -> text_projection -> L2-normalize
  decide: softmax(cosine_similarity(img_emb, txt_emb))

`load_model(ckpt_path, device)` returns a `MammoClipZeroShot` exposing
`zero_shot(pil_image, prompts) -> list[float]` (probability per prompt).

Vendored from batmanlab/Mammo-CLIP — CC BY-NC-SA 4.0 (non-commercial).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import numpy as np
import torch


class MammoClipZeroShot:
    def __init__(self, model, tokenizer, device, *, mean, std, size_h, size_w, max_len):
        self.model = model
        self.tokenizer = tokenizer
        self.device = device
        self.mean = mean
        self.std = std
        self.size_h = size_h  # albumentations Resize(width=size_h, ...) -> output width
        self.size_w = size_w  # -> output height
        self.max_len = max_len

    def _preprocess(self, pil_image) -> torch.Tensor:
        # b5-detect encoder consumes 3-channel RGB (see image_classification_zs.py).
        img = pil_image.convert("RGB")
        # albumentations Resize(width=size_h, height=size_w): PIL.resize takes (w, h).
        img = img.resize((self.size_h, self.size_w))
        arr = np.asarray(img).astype("float32")
        arr -= arr.min()
        mx = float(arr.max())
        if mx > 0:
            arr /= mx
        arr = (arr - self.mean) / self.std
        t = torch.tensor(arr, dtype=torch.float32)  # (H, W, C)
        return t.permute(2, 0, 1).unsqueeze(0).to(self.device)  # (1, C, H, W)

    @torch.no_grad()
    def _image_emb(self, pil_image) -> np.ndarray:
        x = self._preprocess(pil_image)
        emb = self.model.encode_image(x)
        emb = self.model.image_projection(emb) if self.model.projection else emb
        emb = emb / torch.norm(emb, dim=1, keepdim=True)
        return emb.detach().cpu().numpy()

    @torch.no_grad()
    def _text_emb(self, prompts: list[str]) -> np.ndarray:
        tok = self.tokenizer(
            list(prompts), padding="longest", truncation=True,
            return_tensors="pt", max_length=self.max_len,
        ).to(self.device)
        emb = self.model.encode_text(tok)
        emb = self.model.text_projection(emb) if self.model.projection else emb
        emb = emb / torch.norm(emb, dim=1, keepdim=True)
        return emb.detach().cpu().numpy()

    def zero_shot(self, pil_image, prompts: list[str]) -> list[float]:
        """Softmax over image-text cosine similarity for the given prompts.
        Embeddings are already L2-normalized, so cosine == dot product."""
        img = self._image_emb(pil_image)          # (1, D), unit-norm
        txt = self._text_emb(prompts)             # (N, D), unit-norm
        sims = (img @ txt.T)[0]                    # (N,) cosine similarities
        z = sims - sims.max()
        e = np.exp(z)
        probs = e / e.sum()
        return [float(p) for p in probs]


def _prepare_model_cfg(model_cfg: dict, cache_root: str) -> dict:
    """Prepare the model config for a weightless build: point encoder cache_dirs at a
    writable path (the checkpoint's original paths are absolute cluster paths that don't
    exist here) and force pretrained=False. We load the full Mammo-CLIP checkpoint
    state_dict afterward, so base ImageNet/BERT weights are unnecessary — and avoiding
    them removes a fragile network dependency (e.g. Bio_ClinicalBERT weight download)."""
    cfg = copy.deepcopy(model_cfg)
    for enc in ("image_encoder", "text_encoder"):
        if isinstance(cfg.get(enc), dict):
            if "cache_dir" in cfg[enc]:
                cfg[enc]["cache_dir"] = cache_root
            cfg[enc]["pretrained"] = False
    return cfg


def load_model(ckpt_path: str, device) -> MammoClipZeroShot:
    from breastclip.model import build_model
    from transformers import AutoTokenizer

    ckpt = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
    cfg = ckpt["config"]

    cache_root = str(Path(ckpt_path).parent / "hf_cache")
    os.makedirs(cache_root, exist_ok=True)

    tok_name = (
        cfg.get("tokenizer", {}).get("pretrained_model_name_or_path")
        or cfg["model"]["text_encoder"]["name"]
    )
    tokenizer = AutoTokenizer.from_pretrained(tok_name, cache_dir=cache_root)
    if tokenizer.bos_token_id is None:
        tokenizer.bos_token_id = tokenizer.cls_token_id

    model_cfg = _prepare_model_cfg(cfg["model"], cache_root)
    model = build_model(model_cfg, cfg.get("loss", {}), tokenizer)
    model.load_state_dict(ckpt["model"], strict=False)
    model.eval().to(device)

    base = cfg.get("base", {})
    mean = float(base.get("mean", 0.3089279))
    std = float(base.get("std", 0.25053555408335154))
    resize = cfg.get("transform", {}).get("test", {}).get("Resize", {})
    size_h = int(resize.get("size_h", base.get("image_size_h", 1520)))
    size_w = int(resize.get("size_w", base.get("image_size_w", 912)))
    max_len = int(base.get("text_max_length", 256))

    return MammoClipZeroShot(
        model, tokenizer, device,
        mean=mean, std=std, size_h=size_h, size_w=size_w, max_len=max_len,
    )
