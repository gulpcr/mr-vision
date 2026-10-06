from __future__ import annotations

import asyncio
import io
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Two interchangeable backends behind the same public methods:
#
#   "api_key" — google-generativeai + GEMINI_API_KEY (Google AI Studio). Local/dev only:
#               AI Studio is NOT covered by the Google Cloud BAA, so it must never receive
#               PHI in production.
#   "vertex"  — google-genai against Vertex AI (``genai.Client(vertexai=True, ...)``),
#               authenticated with Application Default Credentials (the GCE VM's service
#               account, which needs roles/aiplatform.user). No key. Covered by the BAA.
#
# Select with GEMINI_BACKEND (+ GCP_PROJECT / GCP_LOCATION for vertex). Both backends take
# the same generation-config dicts below (temperature / max_output_tokens /
# response_mime_type are field names in both SDKs).
#
# NOTE on max_output_tokens: the google-generativeai SDK (deprecated) has no way to cap or
# disable Gemini 2.5's internal "thinking" tokens — GenerationConfig doesn't expose a
# thinking_budget field at all (unlike the newer google.genai SDK). Thinking tokens are drawn
# from the SAME max_output_tokens budget as the visible response, and can consume the large
# majority of it (observed: ~2400 thinking tokens vs. ~700 visible tokens for a moderately
# complex prompt) — a cap sized only for the visible JSON truncates mid-response
# (finish_reason=MAX_TOKENS) and produces invalid/unparseable JSON. These caps are sized with
# that hidden overhead in mind, not just the expected visible output length. The vertex
# backend uses the same caps (and the model's default thinking) so both behave alike.
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

# No response_mime_type here, deliberately — forcing "application/json" makes the SDK
# constrain generation to valid JSON syntax, which is incompatible with asking the model
# for free-form Markdown prose. Slightly higher temperature than the structured configs
# above: this path is used for autonomous clinical narrative writing, not strict
# transcription, so some latitude in phrasing/style is intended, not a defect.
_MARKDOWN_REPORT_GENERATION_CONFIG = {
    "temperature": 0.3,
    "max_output_tokens": 8192,
}

_BACKENDS = ("api_key", "vertex")


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


def _image_mime_type(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def _to_genai_part(types: Any, data: bytes) -> Any:
    """Image bytes -> google.genai Part. PNG/JPEG are passed through untouched; anything
    else is re-encoded to PNG with Pillow (the api_key path also decodes via Pillow)."""
    mime = _image_mime_type(data)
    if mime is None:
        from PIL import Image as _PIL_Image

        buf = io.BytesIO()
        _PIL_Image.open(io.BytesIO(data)).save(buf, format="PNG")
        data, mime = buf.getvalue(), "image/png"
    return types.Part.from_bytes(data=data, mime_type=mime)


class GeminiClient:
    """Thin async wrapper around Gemini — AI Studio (API key) or Vertex AI (ADC)."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-1.5-flash",
        *,
        backend: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ):
        self._ready = False
        self._model = None  # google-generativeai GenerativeModel ("api_key")
        self._client = None  # google.genai Client ("vertex")
        self._model_name = model_name

        try:
            from app.config import get_settings

            s = get_settings()
        except Exception:  # settings unavailable (scripts/tests) -> legacy behaviour
            s = None

        # PHI goes to an external provider: in production only under a declared BAA
        # (Settings.external_ai_allowed). Not ready = every caller's existing fallback.
        if s is not None and s.production_mode and not s.external_ai_baa_confirmed:
            logger.warning(
                "external_ai_blocked_no_baa",
                detail="Set EXTERNAL_AI_BAA_CONFIRMED=true once a BAA covers the Gemini provider",
            )
            return

        # Unspecified backend/project/location come from Settings, so every existing
        # ``GeminiClient(api_key=..., model_name=...)`` call site follows GEMINI_BACKEND.
        if s is not None:
            backend = backend if backend is not None else s.gemini_backend
            project = project if project is not None else s.gcp_project
            location = location if location is not None else s.gcp_location
        else:
            backend = backend or "api_key"
        backend = (backend or "api_key").strip().lower()
        if backend not in _BACKENDS:
            logger.warning("gemini_backend_invalid", backend=backend, allowed=_BACKENDS)
            return
        self._backend = backend

        if backend == "vertex":
            self._init_vertex(project or "", location or "us-central1")
        else:
            self._init_api_key(api_key)

    @property
    def backend(self) -> str:
        return getattr(self, "_backend", "")

    def _init_api_key(self, api_key: str) -> None:
        if not api_key:
            logger.warning("gemini_api_key_missing", detail="Set GEMINI_API_KEY to enable LLM reports")
            return

        try:
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(self._model_name)
            self._ready = True
            logger.info("gemini_client_ready", model=self._model_name, backend="api_key")
        except ImportError:
            logger.warning(
                "google_generativeai_not_installed",
                detail="Run: pip install google-generativeai",
            )

    def _init_vertex(self, project: str, location: str) -> None:
        if not project:
            logger.warning(
                "gemini_vertex_project_missing",
                detail="Set GCP_PROJECT (and GCP_LOCATION) when GEMINI_BACKEND=vertex",
            )
            return
        try:
            from google import genai

            self._client = genai.Client(vertexai=True, project=project, location=location)
            self._ready = True
            logger.info(
                "gemini_client_ready", model=self._model_name, backend="vertex",
                project=project, location=location,
            )
        except ImportError:
            logger.warning("google_genai_not_installed", detail="Run: pip install google-genai")
        except Exception as exc:  # e.g. no Application Default Credentials on this host
            logger.warning("gemini_vertex_init_failed", error=str(exc))

    @property
    def ready(self) -> bool:
        return self._ready

    async def _generate(
        self, prompt: str, images: list[bytes] | None, config: dict[str, Any], error_event: str
    ) -> str:
        """One generate_content call on the configured backend; returns stripped text.
        ``images=None`` sends the bare prompt string (text-only)."""
        if not self._ready:
            return ""
        if self._backend == "vertex":
            if self._client is None:
                return ""
            from google.genai import types

            contents: Any = (
                prompt if images is None
                else [prompt, *(_to_genai_part(types, b) for b in images)]
            )
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._model_name,
                contents=contents,
                config=types.GenerateContentConfig(**config),
            )
        else:
            if self._model is None:
                return ""
            if images is None:
                contents = prompt
            else:
                from PIL import Image as _PIL_Image

                contents = [prompt, *(_PIL_Image.open(io.BytesIO(b)) for b in images)]
            response = await asyncio.to_thread(
                self._model.generate_content,
                contents,
                generation_config=config,
            )
        try:
            text = response.text.strip()
        except Exception as exc:
            logger.warning(error_event, error=str(exc))
            text = _extract_text_from_parts(response)
        return text

    async def generate_text(self, prompt: str) -> str:
        """Generate text from prompt. Runs SDK call in thread to avoid blocking the event loop."""
        return await self._generate(prompt, None, _GENERATION_CONFIG, "gemini_response_text_error")

    async def generate_from_image(self, prompt: str, image_bytes: bytes) -> str:
        """Send a prompt + PNG image to Gemini Vision and return the text response."""
        return await self._generate(
            prompt, [image_bytes], _VLM_GENERATION_CONFIG, "gemini_image_response_error"
        )

    async def generate_from_images(self, prompt: str, images: list[bytes]) -> str:
        """Send a prompt + multiple PNG images to Gemini Vision in one call and return the
        text response. Used for multi-view reads (e.g. MIP + fused axial/coronal/sagittal)
        where the model needs to reason across several images together, not one at a time.
        Uses a larger output-token budget than single-image QA calls since the expected
        response (multi-region findings + conclusions) is much longer than a QA verdict.
        """
        return await self._generate(
            prompt, list(images), _REPORT_GENERATION_CONFIG, "gemini_images_response_error"
        )

    async def generate_markdown_from_images(self, prompt: str, images: list[bytes]) -> str:
        """Like ``generate_from_images``, but does NOT force JSON output — used when the
        caller wants free-form Markdown prose (e.g. an autonomously-written clinical
        narrative) rather than a structured object. See
        ``_MARKDOWN_REPORT_GENERATION_CONFIG``.
        """
        return await self._generate(
            prompt, list(images), _MARKDOWN_REPORT_GENERATION_CONFIG,
            "gemini_markdown_response_error",
        )
