from __future__ import annotations

import json
import re
from typing import Any

import structlog

from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

# ── Use-case specific clinical guidance injected into every prompt ────────────

_USECASE_GUIDANCE: dict[str, str] = {
    "brain_mri": (
        "Apply RANO 2.0 criteria for glioma/brain tumor assessment. Key thresholds: "
        "whole_tumor_volume_ml ≥ 100 mL → high risk; ≥ 50 mL → moderate risk. "
        "Note enhancing tumor, necrosis, and perilesional edema extent. "
        "Flag if tumor borders the ventricular system or eloquent cortex (motor, language, visual)."
    ),
    "spine_mri": (
        "Apply Pfirrmann grading for disc degeneration (Grade I–V). For spinal stenosis: "
        "central canal diameter < 10 mm → moderate stenosis; < 6 mm → severe. "
        "Flag foraminal stenosis and any cord signal change (myelopathy). "
        "Note the number of affected levels and their anatomical region (C/T/L spine)."
    ),
    "chest_mri": (
        "Apply ACR pulmonary guidelines. Flag significant bilateral volume asymmetry > 20%. "
        "Note pleural effusion, consolidation, and mass lesions. "
        "For cardiac chambers, reference normal EF range (55–70%) and standard chamber dimensions."
    ),
    "abdomen_mri": (
        "Apply standard organ volume reference ranges: liver 1000–1800 mL (hepatomegaly > 2000 mL), "
        "spleen 150–350 mL (splenomegaly > 500 mL), pancreas 60–120 mL. "
        "Flag organomegaly, focal lesions, biliary dilation, and free fluid."
    ),
    "pet_ct": (
        "Apply PERCIST 1.0 for response assessment and Deauville 5-point scale for lymphoma. "
        "Key thresholds: SUVmax ≥ 20 → CRITICAL; ≥ 15 → high risk. "
        "Deauville score 4–5 → treatment failure. Report MTV and TLG for global disease burden."
    ),
    "pet_ct_brain": (
        "Apply SUVR thresholds for amyloid PET: centiloid ≥ 50 indicates significant amyloid burden. "
        "amyloid_positive == True → clinical significance. "
        "Report global SUVR and regional cortical distribution pattern. "
        "Consider early vs late-stage Alzheimer pathology."
    ),
}

_DEFAULT_GUIDANCE = (
    "Apply standard clinical guidelines for the relevant body part and imaging modality. "
    "Report key measurements relative to published normal ranges. "
    "Flag significant deviations that may require clinical attention."
)

# ── Main prompt template ──────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You are a clinical decision support AI embedded in a radiology AI platform.
Given the quantitative MRI analysis results below, provide structured clinical context.

IMPORTANT: This output is AI-generated decision support only. It must be reviewed by a qualified \
radiologist before influencing any clinical decision.

Study type: {usecase}

{measurements_block}
{summary_block}
{qa_block}

Use-case clinical guidance:
{guidance}

Respond ONLY with a valid JSON object — no markdown, no extra text:
{{
  "risk_level": "<low|moderate|high|critical>",
  "urgency": "<routine|semi-urgent|urgent|emergent>",
  "interpretation": "<2-4 sentence clinical interpretation of the findings>",
  "recommendations": ["<recommendation 1>", "<recommendation 2>"],
  "relevant_criteria": "<guideline or scoring system referenced, e.g. RANO 2.0, PERCIST, Pfirrmann>",
  "disclaimer": "AI-generated clinical decision support — requires radiologist verification before clinical use."
}}

Risk level definitions:
  low      — findings within normal limits or minor incidental note
  moderate — findings requiring clinical attention but not emergency referral
  high     — significant pathology requiring prompt clinical follow-up (days)
  critical — findings requiring immediate clinical action (hours)

Urgency definitions:
  routine    — standard follow-up at next scheduled appointment
  semi-urgent — follow-up within 1–2 weeks
  urgent     — follow-up within 24–48 hours
  emergent   — immediate referral or intervention required
"""

# ── Risk → colour mapping (used by PDF generator) ────────────────────────────

RISK_COLOURS: dict[str, str] = {
    "low": "#16a34a",       # green
    "moderate": "#d97706",  # amber
    "high": "#dc2626",      # red
    "critical": "#7f1d1d",  # dark red
}

URGENCY_COLOURS: dict[str, str] = {
    "routine": "#6b7280",       # gray
    "semi-urgent": "#d97706",   # amber
    "urgent": "#ea580c",        # orange
    "emergent": "#7f1d1d",      # dark red
}

_VALID_RISK_LEVELS = {"low", "moderate", "high", "critical"}
_VALID_URGENCIES = {"routine", "semi-urgent", "urgent", "emergent"}


class ClinicalDecisionService:
    """Generates structured clinical decision support context via Gemini."""

    def __init__(self, client: GeminiClient):
        self._client = client

    @property
    def available(self) -> bool:
        return self._client.ready

    async def generate_clinical_context(
        self,
        usecase_name: str,
        summary: dict[str, Any],
        measurements: dict[str, Any],
        qa_flags: list,
    ) -> dict[str, Any]:
        """
        Generate clinical decision support for a completed pipeline result.

        Returns a dict suitable for storage in Result.summary["clinical_context"].
        Returns an empty dict if the service is unavailable or the call fails.
        """
        if not self._client.ready:
            return {}

        prompt = _build_prompt(usecase_name, summary, measurements, qa_flags)
        try:
            raw = await self._client.generate_text(prompt)
            parsed = _parse_json_response(raw)
            context = _validate_and_normalise(parsed)
            logger.info(
                "cds_generated",
                usecase=usecase_name,
                risk_level=context.get("risk_level"),
                urgency=context.get("urgency"),
            )
            return context
        except Exception as exc:
            logger.error("cds_generation_failed", usecase=usecase_name, error=str(exc))
            return {}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_prompt(
    usecase_name: str,
    summary: dict[str, Any],
    measurements: dict[str, Any],
    qa_flags: list,
) -> str:
    meas_lines = []
    for k, v in measurements.items():
        label = k.replace("_", " ").title()
        meas_lines.append(f"  - {label}: {v:.3f}" if isinstance(v, float) else f"  - {label}: {v}")
    meas_block = ("Quantitative measurements:\n" + "\n".join(meas_lines)) if meas_lines else ""

    # Exclude the clinical_context key itself from the summary block (avoid recursion on re-gen)
    sum_lines = []
    for k, v in summary.items():
        if k == "clinical_context":
            continue
        label = k.replace("_", " ").title()
        sum_lines.append(f"  - {label}: {v}")
    sum_block = ("Summary findings:\n" + "\n".join(sum_lines)) if sum_lines else ""

    flag_strings = [f.value if hasattr(f, "value") else str(f) for f in qa_flags]
    qa_block = ("Image quality / QA flags: " + ", ".join(flag_strings)) if flag_strings else ""

    guidance = _USECASE_GUIDANCE.get(usecase_name, _DEFAULT_GUIDANCE)

    return _PROMPT_TEMPLATE.format(
        usecase=usecase_name.replace("_", " ").title(),
        measurements_block=meas_block,
        summary_block=sum_block,
        qa_block=qa_block,
        guidance=guidance,
    )


def _parse_json_response(text: str) -> dict[str, Any]:
    """Parse JSON from Gemini response, tolerating surrounding prose and markdown fences."""
    text = text.strip()
    # Direct parse (best case: model obeyed the instruction)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip all markdown fences and retry
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Last resort: find the first { and use a string-aware decoder
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = cleaned.find("{", idx)
        if start == -1:
            break
        try:
            obj, _ = decoder.raw_decode(cleaned, start)
            return obj  # type: ignore[return-value]
        except json.JSONDecodeError:
            idx = start + 1
    raise ValueError("No valid JSON object in LLM response")


def _validate_and_normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Clamp enum fields to known values so no invalid string slips into the DB."""
    risk = str(raw.get("risk_level", "moderate")).lower()
    urgency = str(raw.get("urgency", "routine")).lower()

    return {
        "risk_level": risk if risk in _VALID_RISK_LEVELS else "moderate",
        "urgency": urgency if urgency in _VALID_URGENCIES else "routine",
        "interpretation": str(raw.get("interpretation", "")),
        "recommendations": [str(r) for r in raw.get("recommendations", [])],
        "relevant_criteria": str(raw.get("relevant_criteria", "")),
        "disclaimer": str(
            raw.get(
                "disclaimer",
                "AI-generated clinical decision support — requires radiologist verification before clinical use.",
            )
        ),
    }
