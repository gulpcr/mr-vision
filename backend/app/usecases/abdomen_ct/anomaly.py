from __future__ import annotations

"""Anomaly-guided axial-slice selection for the abdomen_ct pipeline.

A MONAI VQ-VAE + autoregressive DecoderOnlyTransformer trained normal-only is used
here as a pure SLICE SELECTOR. For each candidate axial slice we:

  1. window it to the TRAINING intensity range ([-150, 250] HU → [0, 1]) and resize
     to the training input size (128×128);
  2. VQ-VAE ``index_quantize`` → a 16×16 grid of codebook indices;
  3. flatten the grid in the training ``Ordering`` and prepend a BOS token;
  4. run the transformer and take the per-slice mean negative log-likelihood (NLL)
     of the true code sequence — high NLL = the model finds this slice improbable
     = anomalous.

We then non-max-suppress by slice distance and return the top-K RAW z-indices. The
VQ-VAE reconstruction is NEVER produced or shown: the model only tells the pipeline
WHICH raw slices to render for MedGemma.

Self-contained by design (no cross-plugin imports). torch/monai are imported lazily
inside methods so importing this module never requires them; ``load()`` is guarded
by the caller and any failure degrades to the pipeline's even-sampling fallback.
"""

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


class AnomalySliceSelector:
    """Loads the VQVAE+Transformer and ranks axial slices by anomaly NLL."""

    def __init__(self, cfg: dict[str, Any], usecase_dir: Path) -> None:
        self._cfg = cfg or {}
        self._dir = usecase_dir
        self._vqvae = None
        self._transformer = None
        self._device = None          # torch.device
        self._seq_order = None       # LongTensor: latent-grid → sequence permutation
        self._num_embeddings = int(self._cfg.get("vqvae", {}).get("num_embeddings", 256))
        self._input_size = int(self._cfg.get("input_size", 128))
        self._hu_min = float(self._cfg.get("hu_min", -150.0))
        self._hu_max = float(self._cfg.get("hu_max", 250.0))
        self.checksum = "n/a"
        self.version = "abdomen_ct_vqvae_transformer"

    @property
    def ready(self) -> bool:
        return self._vqvae is not None and self._transformer is not None

    # ── model construction ────────────────────────────────────────────────────

    def load(self) -> None:
        """Build both networks and load weights. Raises on any failure (caller guards)."""
        import torch
        from monai.networks.nets import VQVAE, DecoderOnlyTransformer
        from monai.utils.ordering import Ordering, OrderingType

        vq_cfg = self._cfg.get("vqvae", {})
        tr_cfg = self._cfg.get("transformer", {})

        device_str = self._cfg.get("device", "auto")
        if device_str == "auto":
            device_str = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = torch.device(device_str)

        vqvae = VQVAE(
            spatial_dims=2,
            in_channels=1,
            out_channels=1,
            channels=tuple(vq_cfg.get("num_channels", [128, 256, 256])),  # MONAI 1.4 arg name
            num_res_channels=tuple(vq_cfg.get("num_res_channels", [128, 256, 256])),
            num_res_layers=int(vq_cfg.get("num_res_layers", 2)),
            num_embeddings=int(vq_cfg.get("num_embeddings", 256)),
            embedding_dim=int(vq_cfg.get("embedding_dim", 64)),
        )
        transformer = DecoderOnlyTransformer(
            num_tokens=int(tr_cfg.get("num_tokens", 257)),
            max_seq_len=int(tr_cfg.get("max_seq_len", 256)),
            attn_layers_dim=int(tr_cfg.get("attn_layers_dim", 256)),
            attn_layers_depth=int(tr_cfg.get("attn_layers_depth", 16)),
            attn_layers_heads=int(tr_cfg.get("attn_layers_heads", 8)),
            with_cross_attention=False,   # forward then never touches the cross-attn params
        )

        vq_path = self._resolve(self._cfg.get("vqvae_weights", "model/vqvae_last.pt"))
        tr_path = self._resolve(self._cfg.get("transformer_weights", "model/transformer_last.pt"))
        vqvae.load_state_dict(self._read_state(vq_path))       # strict: VQVAE arch matches exactly
        self._load_transformer(transformer, self._read_state(tr_path))

        vqvae.to(self._device).eval()
        transformer.to(self._device).eval()
        self._vqvae = vqvae
        self._transformer = transformer

        # Latent grid = input / 2^(#downsamples). Build the flatten ordering once.
        grid = self._input_size // (2 ** len(vq_cfg.get("num_channels", [128, 256, 256])))
        ordering_name = str(self._cfg.get("ordering", "raster_scan"))
        ordering = Ordering(
            ordering_type=self._ordering_value(OrderingType, ordering_name),
            spatial_dims=2,
            dimensions=(1, grid, grid),
        )
        self._seq_order = torch.as_tensor(
            np.asarray(ordering.get_sequence_ordering()).copy(), dtype=torch.long, device=self._device
        )

        self.checksum = self._sha16(vq_path)
        self.version = f"abdomen_ct_vqvae_transformer_{Path(vq_path).stem}"
        logger.info(
            "abdomen_ct_anomaly_model_loaded",
            device=str(self._device), latent_grid=grid, ordering=ordering_name,
            heads=int(tr_cfg.get("attn_layers_heads", 8)), checksum=self.checksum,
        )

    # ── scoring / selection ────────────────────────────────────────────────────

    def select(self, arr: np.ndarray, top_k: int, z_lo: int, z_hi: int) -> dict[str, Any] | None:
        """Return the ``top_k`` most-anomalous raw z-indices within ``[z_lo, z_hi]``.

        ``arr`` is the raw HU volume (X, Y, Z). Returns ``{selected_z, scores, ...}``
        with ``selected_z`` ordered superior→inferior (descending z, matching the
        pipeline's display convention), or ``None`` if nothing could be scored.
        """
        import torch
        import torch.nn.functional as F

        if not self.ready or arr.ndim != 3:
            return None

        sel_cfg = self._cfg.get("selection", {})
        max_scored = int(sel_cfg.get("max_scored_slices", 400))
        min_sep = int(sel_cfg.get("min_slice_separation", 5))
        min_fg = float(sel_cfg.get("min_foreground_fraction", 0.02))
        batch_size = int(sel_cfg.get("batch_size", 16))

        z_lo = max(0, int(z_lo))
        z_hi = min(arr.shape[2] - 1, int(z_hi))
        if z_hi <= z_lo:
            z_lo, z_hi = 0, arr.shape[2] - 1

        candidates = list(range(z_lo, z_hi + 1))
        if len(candidates) > max_scored:
            step = len(candidates) / max_scored
            candidates = [candidates[int(i * step)] for i in range(max_scored)]
            logger.info("abdomen_ct_anomaly_candidates_strided",
                        total=z_hi - z_lo + 1, scored=len(candidates))

        # Preprocess each candidate to a (1, H, W) tensor; drop near-empty slices.
        tensors: list[Any] = []
        kept_z: list[int] = []
        for z in candidates:
            norm = np.clip(
                (arr[:, :, z].astype(np.float32) - self._hu_min) / max(self._hu_max - self._hu_min, 1e-6),
                0.0, 1.0,
            )
            if float((norm > 0.05).mean()) < min_fg:
                continue
            t = torch.from_numpy(norm)[None, None]  # (1,1,X,Y)
            t = F.interpolate(t, size=(self._input_size, self._input_size),
                              mode="bilinear", align_corners=False)
            tensors.append(t[0])                    # (1,H,W)
            kept_z.append(z)

        if not kept_z:
            logger.warning("abdomen_ct_anomaly_no_scorable_slices")
            return None

        scores: list[float] = []
        with torch.no_grad():
            for i in range(0, len(tensors), batch_size):
                batch = torch.stack(tensors[i:i + batch_size]).to(self._device)  # (B,1,H,W)
                scores.extend(self._batch_nll(batch, F))

        z_score = dict(zip(kept_z, scores))
        selected = self._nms_topk(kept_z, scores, top_k, min_sep)

        logger.info(
            "abdomen_ct_anomaly_selection",
            scored=len(kept_z), selected=selected,
            top_scores=[round(z_score[z], 3) for z in selected],
        )
        return {
            "selected_z": selected,
            "scores": {int(z): round(float(s), 4) for z, s in z_score.items()},
            "scored_count": len(kept_z),
            "model_version": self.version,
            "model_checksum": self.checksum,
        }

    def _batch_nll(self, batch: Any, F: Any) -> list[float]:
        """Mean per-token NLL of the true code sequence for each slice in the batch."""
        idx = self._vqvae.index_quantize(batch)         # (B, h, w) long
        flat = idx.reshape(idx.shape[0], -1)            # (B, S)
        flat = flat[:, self._seq_order]                 # reorder to training sequence
        flat = F.pad(flat, (1, 0), mode="constant", value=self._num_embeddings)  # prepend BOS
        flat = flat.long()
        logits = self._transformer(flat[:, :-1])        # (B, S, num_tokens)
        target = flat[:, 1:]                            # (B, S)
        nll = F.cross_entropy(
            logits.reshape(-1, logits.shape[-1]), target.reshape(-1), reduction="none"
        ).reshape(target.shape[0], -1).mean(dim=1)      # (B,)
        return [float(v) for v in nll.detach().cpu().numpy()]

    @staticmethod
    def _nms_topk(zs: list[int], scores: list[float], k: int, min_sep: int) -> list[int]:
        """Highest-scoring z indices, each ≥ ``min_sep`` apart; returned superior→inferior."""
        order = sorted(range(len(zs)), key=lambda i: scores[i], reverse=True)
        picked: list[int] = []
        for i in order:
            z = zs[i]
            if all(abs(z - p) >= max(1, min_sep) for p in picked):
                picked.append(z)
            if len(picked) >= max(1, k):
                break
        return sorted(picked, reverse=True)  # descending z = superior→inferior

    # ── helpers ────────────────────────────────────────────────────────────────

    def _resolve(self, rel: str) -> str:
        p = Path(rel)
        return str(p if p.is_absolute() else (self._dir / p))

    @staticmethod
    def _load_transformer(model: Any, state: dict[str, Any]) -> None:
        """Load transformer weights, bridging monai-generative → MONAI-core naming.

        The weights were trained with the standalone ``monai-generative`` package,
        whose TransformerBlock names the MLP LayerNorm ``norm3`` and omits cross-
        attention entirely. MONAI-core 1.4 renamed that norm to ``norm2`` and always
        instantiates cross-attn params (``cross_attn`` / ``norm_cross_attn``) which,
        with ``with_cross_attention=False``, the forward pass never uses. So we remap
        ``norm3 → norm2`` and load non-strict, then assert the ONLY unmatched keys are
        those unused cross-attn params — anything else means a real mismatch.
        """
        remapped = {k.replace(".norm3.", ".norm2."): v for k, v in state.items()}
        missing, unexpected = model.load_state_dict(remapped, strict=False)
        stray = [m for m in missing if "cross_attn" not in m]  # norm_cross_attn also matches
        if unexpected or stray:
            raise RuntimeError(
                f"transformer weight mismatch — unexpected={list(unexpected)}, "
                f"missing(non-cross-attn)={stray}"
            )

    @staticmethod
    def _read_state(path: str) -> dict[str, Any]:
        import torch

        state = torch.load(path, map_location="cpu", weights_only=True)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        elif isinstance(state, dict) and "model" in state:
            state = state["model"]
        return state

    @staticmethod
    def _ordering_value(ordering_type_enum: Any, name: str) -> str:
        """Map a config ordering name to the MONAI OrderingType value, defaulting to raster."""
        try:
            return getattr(ordering_type_enum, name.upper()).value
        except Exception:
            return ordering_type_enum.RASTER_SCAN.value

    @staticmethod
    def _sha16(path: str) -> str:
        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()[:16]
