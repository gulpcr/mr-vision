"""Local HuggingFace MedGemma **vision** client for multi-image prompts.

Why this exists alongside :class:`app.infrastructure.llm.medgemma_client.MedGemmaClient`
(the Ollama one): Ollama's ``/api/generate`` does NOT template multi-image prompts for
the Gemma-3/MedGemma vision stack — passing 2+ images derails the model into detection
JSON or ``<start_of_image>`` sentinel echoes. abdomen_ct4's Stage 1 triage needs true
N-image blocks, so it runs the 4B multimodal model directly through ``transformers``,
whose processor interleaves the image tokens correctly.

Design mirrors ``MedGemmaClient`` so the two are interchangeable at the call site
(:class:`app.usecases.abdomen_ct4.multi_agent_pipeline.CTAbdomen4Pipeline` accepts either):

* Same async surface: ``ready`` + ``async generate_from_images(prompt, images)`` /
  ``async generate_text(prompt)``.
* **Non-blocking by contract.** Every method degrades to ``""`` on any failure and never
  raises, so a load/inference failure can never crash the imaging pipeline.
* **Blocking transport, async surface.** ``model.generate`` runs in a worker thread
  (``asyncio.to_thread``) so the coroutine surface matches MedGemmaClient and never
  blocks the event loop the Celery task drives.
* **Resident, not per-job.** The processor+model are cached at MODULE level keyed by
  ``(model_path, device_map)`` so the first job in a worker pays the load cost and every
  later job reuses the in-VRAM model. The load is lazy (first construction), so it
  happens INSIDE the forked Celery child — CUDA is initialised in the child, not the
  pre-fork parent (fork + pre-initialised CUDA is unsafe).

Stage 2 (the text report) stays on the Ollama ``MedGemmaClient`` — text-only prompts
work fine there and the 27B model need not be loaded into the worker's VRAM.
"""
from __future__ import annotations

import asyncio
import io
from typing import Any, Sequence

import structlog

logger = structlog.get_logger(__name__)

# Resident model cache: one (processor, model) per (model_path, device_map) per process.
_MODEL_CACHE: dict[tuple[str, str], tuple[Any, Any]] = {}


def _load_model(model_path: str, device_map: str) -> tuple[Any, Any]:
    """Load (or return cached) processor+model. Raises on failure — caller degrades."""
    key = (model_path, device_map)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    # NOTE: plug in your live weights here — a local dir or an HF repo id both work.
    # AutoModelForImageTextToText is the correct class for the Gemma-3/MedGemma vision
    # stack (AutoModelForCausalLM will not accept images).
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_path)
    model = AutoModelForImageTextToText.from_pretrained(
        model_path,
        device_map=device_map,
        torch_dtype=torch.bfloat16,
    )
    model.eval()
    _MODEL_CACHE[key] = (processor, model)
    logger.info("hf_medgemma_model_loaded", model_path=model_path, device_map=device_map)
    return processor, model


class HFMedGemmaVisionClient:
    """Fail-safe, async wrapper around a local HF MedGemma multimodal model."""

    def __init__(
        self,
        model_path: str,
        device_map: str = "cuda:0",
        *,
        max_new_tokens: int = 256,
    ) -> None:
        self.model_path = model_path
        self.device_map = device_map
        self.max_new_tokens = int(max_new_tokens)
        self._processor: Any = None
        self._model: Any = None
        self._ready = False

        try:
            self._processor, self._model = _load_model(model_path, device_map)
            self._ready = True
            logger.info("hf_medgemma_ready", model_path=model_path, device_map=device_map)
        except Exception as exc:  # missing transformers/accelerate, weights, or CUDA
            logger.warning(
                "hf_medgemma_load_failed",
                model_path=model_path, device_map=device_map, error=str(exc),
            )

    @property
    def ready(self) -> bool:
        return self._ready

    # ── generation (async, MedGemmaClient-compatible surface) ────────────────────

    async def generate_from_images(self, prompt: str, images: Sequence[bytes] | None) -> str:
        """Multimodal completion: prompt + one or more PNG images (raw bytes)."""
        if not self._ready:
            return ""
        return await asyncio.to_thread(self._generate_sync, prompt, list(images or []))

    async def generate_text(self, prompt: str) -> str:
        """Text-only completion (not used by abdomen_ct4 — Stage 2 is Ollama)."""
        if not self._ready:
            return ""
        return await asyncio.to_thread(self._generate_sync, prompt, [])

    # ── blocking implementation ──────────────────────────────────────────────────

    def _generate_sync(self, prompt: str, images: list[bytes]) -> str:
        try:
            import torch
            from PIL import Image

            pil_images = []
            for b in images:
                if not b:
                    continue
                pil_images.append(Image.open(io.BytesIO(b)).convert("RGB"))

            # Gemma-3/MedGemma chat format: images are embedded IN the message content
            # (each as {"type": "image", "image": <PIL>}), interleaved before the text —
            # this is what makes the processor insert the per-image tokens correctly.
            content: list[dict[str, Any]] = [
                {"type": "image", "image": img} for img in pil_images
            ]
            content.append({"type": "text", "text": prompt})
            messages = [{"role": "user", "content": content}]

            inputs = self._processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            ).to(self._model.device)

            input_len = inputs["input_ids"].shape[-1]
            with torch.inference_mode():
                out = self._model.generate(
                    **inputs, max_new_tokens=self.max_new_tokens, do_sample=False
                )
            completion = out[0][input_len:]
            return self._processor.decode(completion, skip_special_tokens=True).strip()
        except Exception as exc:
            logger.warning("hf_medgemma_generate_failed", model_path=self.model_path, error=str(exc))
            return ""
