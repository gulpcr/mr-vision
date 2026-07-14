from __future__ import annotations

import asyncio
import io

import structlog

logger = structlog.get_logger(__name__)

# NOTE on max_output_tokens: this SDK (google-generativeai, deprecated) has no way to cap or
# disable Gemini 2.5's internal "thinking" tokens — GenerationConfig doesn't expose a
# thinking_budget field at all (unlike the newer google.genai SDK). Thinking tokens are drawn
# from the SAME max_output_tokens budget as the visible response, and can consume the large
# majority of it (observed: ~2400 thinking tokens vs. ~700 visible tokens for a moderately
# complex prompt) — a cap sized only for the visible JSON truncates mid-response
# (finish_reason=MAX_TOKENS) and produces invalid/unparseable JSON. These caps are sized with
# that hidden overhead in mind, not just the expected visible output length.
_GENERATION_CONFIG = {
    "temperature": 0.2,
    "max_output_tokens": 8192,
    "response_mime_type": "application/json",
}

_VLM_GENERATION_CONFIG = {
    "temperature": 0.1,
    "max_output_tokens": 4096,
    "response_mime_type": "application/json",
}

_REPORT_GENERATION_CONFIG = {
    "temperature": 0.2,
    "max_output_tokens": 8192,
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
            logger.warning(
                "gemini_api_key_missing", detail="Set GEMINI_API_KEY to enable LLM reports"
            )
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

    async def generate_structured_from_images(
        self,
        prompt: str,
        images: list[bytes],
        response_schema: dict,
        *,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
    ) -> str:
        """Send a prompt + images to Gemini with a response_schema, constraining the reply to
        strictly conform to the given JSON shape (controlled generation / structured output).
        For use cases where the VLM call IS the primary model (e.g. ct_face_neck's per-region
        structured extraction) rather than a supplementary QA/report pass.

        response_schema must use the OpenAPI-subset dict format this SDK version
        (google-generativeai>=0.8.0) accepts: lowercase "type" values
        (object/string/number/integer/boolean/array), "properties", "required", "enum",
        "items", and "nullable" for optional fields — NOT full JSON-Schema (no "$ref", no
        additionalProperties, no ["type", "null"] unions). Build a reduced, schema-compatible
        mirror of any richer JSON-Schema contract rather than passing it through unchanged.
        """
        if not self._ready or self._model is None:
            return ""
        from PIL import Image as _PIL_Image

        pil_imgs = [_PIL_Image.open(io.BytesIO(b)) for b in images]
        generation_config = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": response_schema,
        }
        response = await asyncio.to_thread(
            self._model.generate_content,
            [prompt, *pil_imgs],
            generation_config=generation_config,
        )
        try:
            text = response.text.strip()
        except Exception as exc:
            logger.warning("gemini_structured_response_error", error=str(exc))
            text = _extract_text_from_parts(response)
        return text

    async def generate_from_images(self, prompt: str, images: list[bytes]) -> str:
        """Send a prompt + multiple PNG images to Gemini Vision in one call and return the
        text response. Used for multi-view reads (e.g. MIP + fused axial/coronal/sagittal)
        where the model needs to reason across several images together, not one at a time.
        Uses a larger output-token budget than single-image QA calls since the expected
        response (multi-region findings + conclusions) is much longer than a QA verdict.
        """
        if not self._ready or self._model is None:
            return ""
        from PIL import Image as _PIL_Image

        pil_imgs = [_PIL_Image.open(io.BytesIO(b)) for b in images]

        response = await asyncio.to_thread(
            self._model.generate_content,
            [prompt, *pil_imgs],
            generation_config=_REPORT_GENERATION_CONFIG,
        )
        try:
            text = response.text.strip()
        except Exception as exc:
            logger.warning("gemini_images_response_error", error=str(exc))
            text = _extract_text_from_parts(response)
        return text
