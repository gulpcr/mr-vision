from __future__ import annotations

"""Report-writer for abdomen CT screening results.

This is a SEPARATE, post-hoc step from the screening pipeline: it takes the per-level
findings a pipeline already produced (``[{z, finding}]``) and asks a TEXT model to
reorganize them into a clean, grouped FINDINGS / CONCLUSIONS report. It does NOT change
how the pipeline flags slices, and it is strictly grounded — the prompt forbids the model
from adding, inferring, or inventing any finding not in the input. Fully non-blocking:
returns ``None`` on any failure so the caller can fall back to the raw findings.

The model is injected (any client exposing ``ready`` and ``async generate_text(prompt)``
— MedGemmaClient or GeminiClient), so the writer model is decoupled from the pipeline.
"""

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class AbdomenReportService:
    """Consolidates per-level screening flags into a grounded findings/conclusions report."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "ready", False))

    async def consolidate(
        self,
        *,
        flagged: list[dict[str, Any]],
        study_description: str | None = None,
    ) -> dict[str, str] | None:
        """Return ``{findings, conclusions}`` written from ``flagged`` only, or None.

        ``flagged`` is ``[{z, finding}]`` where each finding may carry a leading
        ``[window]`` tag. The model is told these are its ONLY source of truth.
        """
        if not self.available:
            return None
        prompt = self._build_prompt(flagged, study_description)
        try:
            raw = await self._client.generate_text(prompt)
        except Exception as exc:  # never propagate — report writing is best-effort
            logger.warning("abdomen_report_generate_failed", error=str(exc))
            return None
        return self._parse(raw)

    # ── prompt / parse ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_prompt(flagged: list[dict[str, Any]], study_description: str | None) -> str:
        context = f' The study is described as "{study_description}".' if study_description else ""
        if flagged:
            lines = "\n".join(
                f"  z={f.get('z')}: {AbdomenReportService._clean(str(f.get('finding', '')))}"
                for f in flagged if f.get("finding")
            ) or "  (levels flagged without a specific description)"
            body = (
                "Below is the COMPLETE list of findings a screening AI flagged on specific axial "
                "CT levels (z index). This list is your ONLY source of information:\n"
                f"{lines}\n\n"
                "Write the FINDINGS and CONCLUSIONS of the report FROM THIS LIST ONLY:\n"
                "- Use ONLY the findings above. Do NOT add, infer, or invent any finding, "
                "measurement, HU value, size, or diagnosis that is not listed.\n"
                "- Merge duplicates and the SAME finding seen on several adjacent levels into ONE "
                "statement, giving its z-range (e.g. \"right adnexal mass (z231-240)\").\n"
                "- Group by organ/region and write flowing prose — NOT a per-level list.\n"
                "- Do not include a finding for a level that was not flagged."
            )
        else:
            body = (
                "A screening AI reviewed the whole volume and flagged NO level as abnormal. "
                "Write a report stating that no focal abnormality was identified on the reviewed "
                "levels. Do not invent any finding."
            )

        return f"""You are a radiologist writing the FINDINGS and CONCLUSIONS sections of an \
ABDOMEN/PELVIS CT report.{context} No clinical history is assumed.

{body}

This is an AI-assisted read limited to the reviewed levels; note that briefly.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<grounded findings, grouped by finding/organ with z-ranges; flowing prose>",
  "conclusions": "<concise conclusion listing the distinct findings + any recommended correlation; 1-3 sentences>"
}}
"""

    @staticmethod
    def _parse(raw: str) -> dict[str, str] | None:
        if not raw:
            return None
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        else:
            brace = re.search(r"\{.*\}", text, re.DOTALL)
            if brace:
                text = brace.group(0)
        try:
            data = json.loads(text)
        except Exception as exc:
            logger.warning("abdomen_report_parse_failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        findings = str(data.get("findings", "") or "").strip()
        conclusions = str(data.get("conclusions", "") or data.get("impression", "") or "").strip()
        if not findings and not conclusions:
            return None
        return {"findings": findings, "conclusions": conclusions}

    @staticmethod
    def _clean(s: str) -> str:
        """Strip the leading ``[window]`` provenance tag from a flag description."""
        return re.sub(r"^\s*\[[^\]]*\]\s*", "", s).strip()
