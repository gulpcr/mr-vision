from __future__ import annotations

import asyncio
import io

import structlog

logger = structlog.get_logger(__name__)

_GENERATION_CONFIG = {
    "temperature": 0.2,
    "max_output_tokens": 2048,
    "response_mime_type": "application/json",
}

_VLM_GENERATION_CONFIG = {
    "temperature": 0.1,
    "max_output_tokens": 1024,
    "response_mime_type": "application/json",
}


def _extract_text_from_parts(response: object) -> str:
    """Fallback: walk response.candidates[0].content.parts filtering out thought parts."""
    try:
        parts = response.candidates[0].content.parts  # type: ignore[attr-defined]
        texts = []
        for part in parts:
            if getattr(part, "thought", False):
                continue
            t = getattr(part, "text", None)
            if t:
                texts.append(t)
        return "".join(texts).strip()
    except Exception as exc:
        print(f"[GEMINI_PARTS_ERROR] {exc}", flush=True)
        return ""


class GeminiClient:
    """Thin wrapper around google-generativeai for async text generation."""

    def __init__(self, api_key: str, model_name: str = "gemini-1.5-flash"):
        self._ready = False
        self._model = None

        if not api_key:
            logger.warning("gemini_api_key_missing", detail="Set GEMINI_API_KEY to enable LLM reports")
            return

        try:
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(model_name)
            self._ready = True
            logger.info("gemini_client_ready", model=model_name)
        except ImportError:
            logger.warning(
                "google_generativeai_not_installed",
                detail="Run: pip install google-generativeai",
            )

    @property
    def ready(self) -> bool:
        return self._ready

    async def generate_text(self, prompt: str) -> str:
        """Generate text from prompt. Runs SDK call in thread to avoid blocking the event loop."""
        if not self._ready or self._model is None:
            return ""
        response = await asyncio.to_thread(
            self._model.generate_content,
            prompt,
            generation_config=_GENERATION_CONFIG,
        )
        try:
            text = response.text.strip()
        except Exception as exc:
            logger.warning("gemini_response_text_error", error=str(exc))
            text = _extract_text_from_parts(response)
        return text

    async def generate_from_image(self, prompt: str, image_bytes: bytes) -> str:
        """Send a prompt + PNG image to Gemini Vision and return the text response."""
        if not self._ready or self._model is None:
            return ""
        from PIL import Image as _PIL_Image

        pil_img = _PIL_Image.open(io.BytesIO(image_bytes))

        response = await asyncio.to_thread(
            self._model.generate_content,
            [prompt, pil_img],
            generation_config=_VLM_GENERATION_CONFIG,
        )
        try:
            text = response.text.strip()
        except Exception as exc:
            logger.warning("gemini_image_response_error", error=str(exc))
            text = _extract_text_from_parts(response)
        return text
