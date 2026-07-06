"""Inference adapter over the vendored Mammo-CLIP RetinaNet detectors.

Loads the released single-class detectors (Mass, Suspicious Calcification) built on the
Mammo-CLIP B5 backbone and runs them for localized detection. Reproduces the upstream
detector preprocessing (Datasets/dataset_concepts.py + dataset_utils.py):

    grayscale -> RGB -> resize 512x512 -> ToTensor (/255) -> per-image min-max ->
    (x - mean) / std   (mean 0.3089279, std 0.25053555)

`load_detectors(detector_dir, device, ...) -> MammoDetector`. The backbone is built
with clip_chk_pt=None (random init) and the trained detector .pth is loaded strict=False,
so no 1.6 GB clip tar reload is needed per build.

Vendored from batmanlab/Mammo-CLIP — CC BY-NC-SA 4.0 (non-commercial).
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import torch

_MEAN = 0.3089279
_STD = 0.25053555408335154
_SIZE = 512  # detector train/eval input (train_detector.py --resize)

# checkpoint subdir -> structured slot name
_DETECTORS = {"mass": "mass", "calc": "calcification"}


class MammoDetector:
    def __init__(self, models: dict, device, *, size=_SIZE, mean=_MEAN, std=_STD, threshold=0.2):
        self.models = models          # {"mass": RetinaNet, "calcification": RetinaNet}
        self.device = device
        self.size = size
        self.mean = mean
        self.std = std
        self.threshold = threshold

    def _preprocess(self, pil_image):
        img = pil_image.convert("RGB").resize((self.size, self.size))
        arr = np.asarray(img).astype("float32") / 255.0   # ToTensor scaling
        arr -= arr.min()
        mx = float(arr.max())
        if mx > 0:
            arr /= mx
        arr = (arr - self.mean) / self.std
        t = torch.from_numpy(np.ascontiguousarray(arr)).permute(2, 0, 1).unsqueeze(0)
        return t.float().to(self.device)

    @torch.no_grad()
    def detect(self, pil_image) -> dict:
        """Return {slot: [{"score": float, "box": [x1,y1,x2,y2] in 0..1}]} for boxes
        above threshold, per detector."""
        x = self._preprocess(pil_image)
        out: dict[str, list] = {}
        for slot, model in self.models.items():
            model.training = False
            scores, labels, boxes = model(x)
            dets = []
            if scores is not None and len(scores) > 0:
                s = scores.detach().cpu().numpy()
                b = boxes.detach().cpu().numpy() / float(self.size)  # -> [0,1]
                for i in range(len(s)):
                    if float(s[i]) >= self.threshold:
                        dets.append({"score": round(float(s[i]), 3),
                                     "box": [float(v) for v in b[i][:4]]})
            out[slot] = sorted(dets, key=lambda d: -d["score"])
        return out


def _build_retinanet(device):
    from Detectors.retinanet.detector_model import RetinaNet_efficientnet

    model = RetinaNet_efficientnet(
        num_classes=1, model_type="breast_clip_b5",
        focal_alpha=0.25, focal_gamma=2.0, clip_chk_pt=None, freeze_backbone="n",
    )
    return model.to(device)


def _find_ckpt(detector_dir: str, subdir: str) -> str | None:
    hits = glob.glob(os.path.join(detector_dir, subdir, "**", "*.pth"), recursive=True)
    return hits[0] if hits else None


def load_detectors(detector_dir: str, device, threshold: float = 0.2) -> MammoDetector:
    models = {}
    for subdir, slot in _DETECTORS.items():
        ckpt = _find_ckpt(detector_dir, f"{subdir}_n") or _find_ckpt(detector_dir, subdir)
        if not ckpt:
            continue
        model = _build_retinanet(device)
        state = torch.load(ckpt, map_location="cpu", weights_only=False)
        sd = state.get("state_dict", state) if isinstance(state, dict) else state
        model.load_state_dict(sd, strict=False)
        model.eval()
        models[slot] = model
    if not models:
        raise FileNotFoundError(f"No detector .pth found under {detector_dir}")
    return MammoDetector(models, device, threshold=threshold)
