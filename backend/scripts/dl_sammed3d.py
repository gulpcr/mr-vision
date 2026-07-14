import os

from huggingface_hub import hf_hub_download

out_dir = "/app/external/SAM-Med3D-main/ckpt"
os.makedirs(out_dir, exist_ok=True)
for fname in ("sam_med3d_turbo.pth",):
    p = hf_hub_download(
        "blueyo0/SAM-Med3D", fname, local_dir=out_dir,
        token=os.environ.get("HF_TOKEN"),
    )
    print("CKPT", p, os.path.getsize(p))
