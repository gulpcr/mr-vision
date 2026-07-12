#!/usr/bin/env python
"""Validate the abdomen_ct VQVAE+Transformer anomaly selector loads and runs.

Run INSIDE a container that has monai==1.4.0 (backend or worker):

    docker compose exec backend python scripts/validate_abdomen_ct_anomaly.py

Confirms: (1) both networks reconstruct from inference_config.yaml, (2) the weights
load with strict=True (proves num_embeddings/dims/depth are right — a wrong
attn_layers_heads still loads here but would corrupt scores, so it is only checked
for divisibility), (3) a dummy slice scores end-to-end. Exit 0 = OK.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

USECASE = Path(__file__).resolve().parents[1] / "app" / "usecases" / "abdomen_ct"


def main() -> int:
    import torch  # noqa: F401  (proves the torch/monai stack is importable)

    cfg = (yaml.safe_load((USECASE / "model" / "inference_config.yaml").read_text()) or {}).get("anomaly", {})
    if not cfg:
        print("FAIL: no anomaly: block in inference_config.yaml")
        return 1

    tr = cfg.get("transformer", {})
    dim, heads = int(tr.get("attn_layers_dim", 256)), int(tr.get("attn_layers_heads", 8))
    if dim % heads:
        print(f"FAIL: attn_layers_dim {dim} not divisible by attn_layers_heads {heads}")
        return 1

    sys.path.insert(0, str(USECASE.parents[2]))  # backend/ (== /app) on path for `app.` import
    from app.usecases.abdomen_ct.anomaly import AnomalySliceSelector

    sel = AnomalySliceSelector(cfg, USECASE)
    try:
        sel.load()  # AnomalySliceSelector loads with strict=True
    except Exception as exc:
        print(f"FAIL: model load: {exc}")
        return 1
    print(f"OK: loaded on {sel._device}, checksum {sel.checksum}, heads={heads}")

    # Dummy volume: 40 axial slices of 256x256 HU, one hyperdense 'lesion' blob.
    import numpy as np

    vol = np.random.default_rng(0).normal(30, 40, size=(256, 256, 40)).astype(np.float32)
    vol[100:140, 100:140, 18:24] = 300.0
    out = sel.select(vol, top_k=4, z_lo=3, z_hi=36)
    if not out or not out.get("selected_z"):
        print("FAIL: select() returned nothing")
        return 1
    print(f"OK: selected z={out['selected_z']}  (scored {out['scored_count']} slices)")
    print(f"    sample scores: {dict(list(out['scores'].items())[:5])}")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
