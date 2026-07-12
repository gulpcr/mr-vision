"""MedGemma client backed by a local Ollama server.

MedGemma is Google's medical-domain Gemma variant. This client talks to a local
Ollama daemon (``ollama pull medgemma1.5``) over its native HTTP API, so no data
leaves the host — the intended deployment for PHI-bearing PET/CT imaging where a
cloud VLM (Gemini) is not acceptable.

It is a **drop-in for** :class:`app.infrastructure.llm.gemini_client.GeminiClient`:
same ``ready`` property and same ``async generate_from_images(prompt, images)`` /
``async generate_text(prompt)`` surface, so services written against GeminiClient
(e.g. :class:`PetCtNarrativeService`) can accept either without change.

Design notes
------------
* **Blocking transport, async surface.** Ollama calls are synchronous ``httpx``
  requests run in a worker thread (``asyncio.to_thread``) so the coroutine
  surface matches GeminiClient and never blocks the event loop the Celery task
  drives.
* **Non-blocking by contract.** Every method degrades to ``""`` on any failure
  and never raises, so a MedGemma outage can never crash the imaging pipeline.
* **JSON mode.** When ``force_json`` is set, the Ollama ``format: "json"`` option
  constrains generation to a valid JSON object — used for the structured
  ``ai_report`` the narrative service expects.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MedGemmaClient:
    """Thin, fail-safe, async wrapper around a local Ollama MedGemma model."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model_name: str = "medgemma1.5:latest",
        timeout_s: int = 300,
        force_json: bool = False,
        num_ctx: int = 8192,
    ) -> None:
        self._base_url = (base_url or "http://localhost:11434").rstrip("/")
        self._model = model_name or "medgemma1.5:latest"
        self._timeout_s = int(timeout_s) if timeout_s else 300
        self._force_json = force_json
        # Ollama context window for this client. MedGemma (Gemma-3) supports up to 128K;
        # 8192 is the safe default. A caller with a large prompt (e.g. a big data matrix,
        # or many images at ~256 tokens each) can widen it here to avoid silent
        # truncation. Larger num_ctx = more KV-cache VRAM, so it is per-client, not global.
        self._num_ctx = int(num_ctx) if num_ctx else 8192
        self._ready = False

        try:
            import httpx  # noqa: F401
        except ImportError:
            logger.warning(
                "httpx_not_installed",
                detail="Run: pip install httpx (required for MedGemma/Ollama)",
            )
            return

        self._ready = self._probe()

    # ── readiness ──────────────────────────────────────────────────────────────

    def _probe(self) -> bool:
        """True when the Ollama daemon is reachable and the model tag is present."""
        import httpx

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                tags = resp.json().get("models", []) or []
        except Exception as exc:
            logger.warning("medgemma_ollama_unreachable", base_url=self._base_url, error=str(exc))
            return False

        # Match the configured tag, tolerating the implicit ":latest" suffix
        # (config "medgemma1.5" == installed "medgemma1.5:latest").
        installed = {str(m.get("name", "")) for m in tags}
        base = self._model.split(":")[0]
        wanted = {self._model, base, f"{base}:latest"}
        if installed & wanted:
            logger.info("medgemma_client_ready", model=self._model, base_url=self._base_url)
            return True

        logger.warning(
            "medgemma_model_not_pulled",
            model=self._model, installed=sorted(installed),
            detail=f"Run: ollama pull {self._model}",
        )
        return False

    @property
    def ready(self) -> bool:
        return self._ready

    # ── generation (async, GeminiClient-compatible surface) ──────────────────────

    async def generate_text(self, prompt: str) -> str:
        """Text-only completion. Returns "" on any failure."""
        if not self._ready:
            return ""
        return await asyncio.to_thread(self._generate_sync, prompt, None)

    async def generate_from_images(self, prompt: str, images: list[bytes] | None) -> str:
        """Multimodal completion: prompt + one or more PNG images (raw bytes).

        Signature-compatible with ``GeminiClient.generate_from_images`` so the
        narrative service can use either provider. Returns "" on any failure.
        """
        if not self._ready:
            return ""
        return await asyncio.to_thread(self._generate_sync, prompt, images)

    # ── blocking implementation ──────────────────────────────────────────────────

    def _generate_sync(self, prompt: str, images: list[bytes] | None) -> str:
        import httpx

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            # Model-call mechanics (kept in code, not site config): deterministic-
            # leaning for clinical transcription; context width is per-client (num_ctx)
            # so image-heavy callers can widen it without changing the global default.
            "options": {"temperature": 0.1, "num_ctx": self._num_ctx},
        }
        if self._force_json:
            payload["format"] = "json"
        if images:
            payload["images"] = [base64.b64encode(b).decode("ascii") for b in images if b]

        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.post(f"{self._base_url}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("medgemma_generate_failed", model=self._model, error=str(exc))
            return ""

        text = str(data.get("response", "") or "").strip()
        if not text:
            logger.warning("medgemma_empty_response", model=self._model)
        return text
