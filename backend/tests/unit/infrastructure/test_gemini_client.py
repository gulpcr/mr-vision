"""GeminiClient backend routing: "api_key" (google-generativeai) vs "vertex" (google-genai).

Both SDKs are replaced with fakes in sys.modules — no network, no credentials.
"""
from __future__ import annotations

import io
import sys
import types as pytypes
from typing import Any

import pytest

from app.config import Settings, get_settings
from app.infrastructure.llm import gemini_client as gc
from app.infrastructure.llm.gemini_client import GeminiClient


class _Resp:
    def __init__(self, text: Any = "  {\"ok\": true}  ", parts: list[Any] | None = None):
        self.text = text
        self.candidates = [
            pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=parts or []))
        ]


@pytest.fixture
def legacy_sdk(monkeypatch):
    """Fake google.generativeai."""
    calls: dict[str, Any] = {"configure": [], "models": [], "generate": []}

    class GenerativeModel:
        def __init__(self, name: str):
            calls["models"].append(name)

        def generate_content(self, contents, generation_config=None):
            calls["generate"].append((contents, generation_config))
            return _Resp()

    mod = pytypes.ModuleType("google.generativeai")
    mod.configure = lambda api_key: calls["configure"].append(api_key)  # type: ignore[attr-defined]
    mod.GenerativeModel = GenerativeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.generativeai", mod)
    if "google" in sys.modules:
        monkeypatch.setattr(sys.modules["google"], "generativeai", mod, raising=False)
    return calls


@pytest.fixture
def vertex_sdk(monkeypatch):
    """Fake google.genai (+ google.genai.types)."""
    calls: dict[str, Any] = {"clients": [], "generate": [], "response": _Resp()}

    class _Models:
        def generate_content(self, *, model, contents, config):
            calls["generate"].append({"model": model, "contents": contents, "config": config})
            return calls["response"]

    class Client:
        def __init__(self, **kwargs):
            calls["clients"].append(kwargs)
            self.models = _Models()

    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Part:
        def __init__(self, data: bytes, mime_type: str):
            self.data, self.mime_type = data, mime_type

        @classmethod
        def from_bytes(cls, *, data: bytes, mime_type: str) -> "Part":
            return cls(data, mime_type)

    types_mod = pytypes.ModuleType("google.genai.types")
    types_mod.GenerateContentConfig = GenerateContentConfig  # type: ignore[attr-defined]
    types_mod.Part = Part  # type: ignore[attr-defined]
    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.Client = Client  # type: ignore[attr-defined]
    genai_mod.types = types_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    if "google" in sys.modules:
        monkeypatch.setattr(sys.modules["google"], "genai", genai_mod, raising=False)
    return calls


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (4, 4)).save(buf, format="PNG")
    return buf.getvalue()


def _bmp() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="BMP")
    return buf.getvalue()


async def test_api_key_backend_uses_generativeai(legacy_sdk, vertex_sdk):
    client = GeminiClient("secret-key", "gemini-x", backend="api_key")

    assert client.ready and client.backend == "api_key"
    assert legacy_sdk["configure"] == ["secret-key"]
    assert legacy_sdk["models"] == ["gemini-x"]
    assert vertex_sdk["clients"] == []  # Vertex never touched

    assert await client.generate_text("hello") == '{"ok": true}'
    contents, config = legacy_sdk["generate"][-1]
    assert contents == "hello"  # text-only: bare prompt string, as before
    assert config == gc._GENERATION_CONFIG

    await client.generate_markdown_from_images("describe", [_png(), _png()])
    contents, config = legacy_sdk["generate"][-1]
    assert contents[0] == "describe" and len(contents) == 3
    assert config == gc._MARKDOWN_REPORT_GENERATION_CONFIG
    assert vertex_sdk["generate"] == []


async def test_api_key_backend_without_key_is_not_ready(legacy_sdk):
    client = GeminiClient("", "gemini-x", backend="api_key")
    assert not client.ready
    assert await client.generate_text("hello") == ""
    assert legacy_sdk["configure"] == []


async def test_vertex_backend_uses_genai_client(legacy_sdk, vertex_sdk):
    client = GeminiClient(
        "ignored-key", "gemini-x", backend="vertex", project="proj-1", location="europe-west4"
    )

    assert client.ready and client.backend == "vertex"
    assert vertex_sdk["clients"] == [
        {"vertexai": True, "project": "proj-1", "location": "europe-west4"}
    ]
    assert legacy_sdk["configure"] == []  # no API key is ever sent in vertex mode

    assert await client.generate_text("hello") == '{"ok": true}'
    call = vertex_sdk["generate"][-1]
    assert call["model"] == "gemini-x"
    assert call["contents"] == "hello"
    assert call["config"].kwargs == gc._GENERATION_CONFIG

    png = _png()
    await client.generate_from_image("qa", png)
    call = vertex_sdk["generate"][-1]
    assert call["contents"][0] == "qa"
    part = call["contents"][1]
    assert part.mime_type == "image/png" and part.data == png  # passed through untouched
    assert call["config"].kwargs == gc._VLM_GENERATION_CONFIG

    await client.generate_from_images("read", [png, _bmp()])
    call = vertex_sdk["generate"][-1]
    assert [p.mime_type for p in call["contents"][1:]] == ["image/png", "image/png"]
    assert call["config"].kwargs == gc._REPORT_GENERATION_CONFIG
    assert legacy_sdk["generate"] == []


async def test_vertex_backend_without_project_is_not_ready(vertex_sdk):
    client = GeminiClient("", "gemini-x", backend="vertex", project="", location="us-central1")
    assert not client.ready
    assert await client.generate_text("hello") == ""
    assert vertex_sdk["clients"] == []


async def test_vertex_empty_text_falls_back_to_non_thought_parts(vertex_sdk):
    vertex_sdk["response"] = _Resp(
        text=None,
        parts=[
            pytypes.SimpleNamespace(thought=True, text="thinking..."),
            pytypes.SimpleNamespace(thought=False, text="answer"),
        ],
    )
    client = GeminiClient("", "gemini-x", backend="vertex", project="p", location="l")
    assert await client.generate_text("q") == "answer"


async def test_unknown_backend_is_not_ready(legacy_sdk, vertex_sdk):
    client = GeminiClient("k", "gemini-x", backend="bogus", project="p", location="l")
    assert not client.ready
    assert await client.generate_text("q") == ""
    assert legacy_sdk["configure"] == [] and vertex_sdk["clients"] == []


async def test_backend_defaults_come_from_settings(monkeypatch, legacy_sdk, vertex_sdk):
    """Existing call sites pass only api_key/model_name; GEMINI_BACKEND decides."""
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("GCP_PROJECT", "from-env")
    monkeypatch.setenv("GCP_LOCATION", "us-east4")
    get_settings.cache_clear()
    try:
        client = GeminiClient(api_key="k", model_name="gemini-x")
    finally:
        get_settings.cache_clear()

    assert client.backend == "vertex"
    assert vertex_sdk["clients"] == [
        {"vertexai": True, "project": "from-env", "location": "us-east4"}
    ]
    assert legacy_sdk["configure"] == []


def test_gemini_configured_property():
    assert Settings(gemini_backend="api_key", gemini_api_key="k").gemini_configured
    assert not Settings(gemini_backend="api_key", gemini_api_key="").gemini_configured
    assert Settings(gemini_backend="vertex", gcp_project="p", gemini_api_key="").gemini_configured
    assert not Settings(gemini_backend="vertex", gcp_project="", gemini_api_key="k").gemini_configured


async def test_production_without_baa_declaration_never_contacts_the_provider(
    monkeypatch, legacy_sdk, vertex_sdk
):
    """PHI goes to Gemini in production only once EXTERNAL_AI_BAA_CONFIRMED is set."""
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    monkeypatch.setenv("GEMINI_BACKEND", "api_key")
    monkeypatch.delenv("EXTERNAL_AI_BAA_CONFIRMED", raising=False)
    get_settings.cache_clear()
    try:
        client = GeminiClient(api_key="k", model_name="gemini-x")
        assert not client.ready
        assert await client.generate_text("patient data") == ""
        assert legacy_sdk["configure"] == [] and legacy_sdk["generate"] == []

        monkeypatch.setenv("EXTERNAL_AI_BAA_CONFIRMED", "true")
        get_settings.cache_clear()
        assert GeminiClient(api_key="k", model_name="gemini-x").ready
    finally:
        get_settings.cache_clear()
