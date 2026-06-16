from __future__ import annotations

from typing import Any

import structlog

from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

_PROMPT_TEMPLATE = """\
You are a radiology AI assistant. Given structured MRI analysis results below, \
write a professional clinical impression paragraph for inclusion in a radiology report.

Study type: {usecase}

{measurements_block}
{summary_block}
{qa_block}

Instructions:
- Write 3-5 sentences in concise clinical language (no bullet points)
- Reference key quantitative measurements with units where available
- Note any image quality issues if QA flags are present
- End with this exact sentence on its own line:
  "AI-generated impression — requires radiologist verification before clinical use."
"""


class LLMReportService:
    """Generates narrative radiology impressions via Gemini."""

    def __init__(self, client: GeminiClient):
        self._client = client

    @property
    def available(self) -> bool:
        return self._client.ready

    async def generate_impression(
        self,
        usecase_name: str,
        summary: dict[str, Any],
        measurements: dict[str, Any],
        qa_flags: list,
    ) -> str:
        """Return a narrative impression string, or empty string on failure/unavailability."""
        if not self._client.ready:
            return ""

        prompt = _build_prompt(usecase_name, summary, measurements, qa_flags)
        try:
            narrative = await self._client.generate_text(prompt)
            logger.info("llm_impression_generated", usecase=usecase_name, chars=len(narrative))
            return narrative
        except Exception as exc:
            logger.error("llm_impression_failed", usecase=usecase_name, error=str(exc))
            return ""


def _build_prompt(
    usecase_name: str,
    summary: dict[str, Any],
    measurements: dict[str, Any],
    qa_flags: list,
) -> str:
    meas_lines = []
    for k, v in measurements.items():
        label = k.replace("_", " ").title()
        formatted = f"{v:.3f}" if isinstance(v, float) else str(v)
        meas_lines.append(f"  - {label}: {formatted}")
    meas_block = ("Quantitative measurements:\n" + "\n".join(meas_lines)) if meas_lines else ""

    sum_lines = []
    for k, v in summary.items():
        label = k.replace("_", " ").title()
        sum_lines.append(f"  - {label}: {v}")
    sum_block = ("Summary findings:\n" + "\n".join(sum_lines)) if sum_lines else ""

    flag_strings = [f.value if hasattr(f, "value") else str(f) for f in qa_flags]
    qa_block = ("QA / image quality flags: " + ", ".join(flag_strings)) if flag_strings else ""

    return _PROMPT_TEMPLATE.format(
        usecase=usecase_name.replace("_", " ").title(),
        measurements_block=meas_block,
        summary_block=sum_block,
        qa_block=qa_block,
    )
