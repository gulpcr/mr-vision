import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import torch
from train_vindr_classifier import MammoClipFinetune

ckpt = r"C:/model_cache/mammo_clip/b5-model-best-epoch-7.tar"
device = "cuda" if torch.cuda.is_available() else "cpu"
print("device:", device)
m = MammoClipFinetune(ckpt).to(device)
m.train()
x = torch.randn(2, 3, 912, 1520, device=device)
with torch.autocast(device_type="cuda", dtype=torch.float16):
    out = m(x)
print("out shapes:", {k: tuple(v.shape) for k, v in out.items()})
print("out_dim:", m.out_dim)
n_train = sum(p.numel() for p in m.parameters() if p.requires_grad)
print("trainable params (heads-only warmup off):", n_train)
m.set_encoder_trainable(True)
n_train = sum(p.numel() for p in m.parameters() if p.requires_grad)
print("trainable params (full):", n_train)
print("OK")
