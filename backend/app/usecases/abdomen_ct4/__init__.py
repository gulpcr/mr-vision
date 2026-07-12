"""abdomen_ct4 — two-stage, two-model local multi-agent pan-abdominal CT screening.

Runs on the same local Ollama daemon as ct2/ct3 (no HF weight loading):

Stage 1 (4B multimodal MedGemma tag): high-speed volumetric triage over overlapping
slice blocks — flags which slices carry any pathology.
Stage 2 (27B MedGemma tag, text-only): synthesises the flagged blocks' text findings
into a single, running-history-aware unified radiological report.

The engine is :class:`CTAbdomen4Pipeline` in :mod:`multi_agent_pipeline`; it takes two
injected ``MedGemmaClient``-compatible clients. :class:`MockMedGemmaClient` is a
deterministic drop-in for standalone runs without Ollama.
"""
from __future__ import annotations

from app.usecases.abdomen_ct4.multi_agent_pipeline import (
    CTAbdomen4Pipeline,
    MockMedGemmaClient,
    PathologyZone,
    PipelineReport,
    TriageResult,
)

__all__ = [
    "CTAbdomen4Pipeline",
    "MockMedGemmaClient",
    "PathologyZone",
    "PipelineReport",
    "TriageResult",
]
