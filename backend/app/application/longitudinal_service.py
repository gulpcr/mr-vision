from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Trend colours used by the PDF generator
TREND_COLOURS: dict[str, str] = {
    "regression": "#16a34a",
    "stable": "#2563eb",
    "progression": "#dc2626",
    "mixed": "#d97706",
    "insufficient_data": "#6b7280",
}

_VALID_TRENDS = {"progression", "regression", "stable", "mixed", "insufficient_data"}
_VALID_RESPONSES = {"CR", "PR", "SD", "PD", "not_applicable"}

_USECASE_GUIDANCE: dict[str, str] = {
    "brain_mri": (
        "Apply RANO 2.0 criteria. CR: complete resolution of enhancing lesion. "
        "PR: ≥50% decrease in sum of products of perpendicular diameters (SPD). "
        "PD: ≥25% increase in SPD or any new lesion. SD: all others. "
        "Key metrics: enhancing_volume_ml, total_lesion_volume_ml, edema_volume_ml."
    ),
    "spine_mri": (
        "Track Pfirrmann grade changes (I–V) per disc level, disc height index, "
        "and central canal diameter (mm). Progression: increasing Pfirrmann grade "
        "or decreasing canal diameter. Key thresholds: canal <10 mm = moderate stenosis, "
        "<6 mm = severe. Flag new cord signal changes."
    ),
    "chest_mri": (
        "Track lobe-level volume changes and bilateral asymmetry. "
        "Clinically significant: >20% volume change in any lobe, or >15% asymmetry shift. "
        "Flag new consolidations, pleural effusion changes, or mass growth."
    ),
    "abdomen_mri": (
        "Track organ volumes: liver (1 000–1 800 mL normal), spleen (150–350 mL), "
        "pancreas (60–120 mL). Flag >20% change from prior. "
        "Note lesion dimensions (longest diameter) and compare to prior. "
        "Apply RECIST 1.1 for measurable target lesions if present."
    ),
    "pet_ct": (
        "Apply PERCIST 1.0. CMR: complete metabolic response (peak SUL < liver mean SUL + 2 SD). "
        "PMR: ≥30% decrease in peak SUL. SMD: <30% decrease and <20% increase. "
        "PMD: ≥20% increase or new FDG-avid lesion. "
        "Also apply Deauville 5-point scale for lymphoma. Key metric: SUVmax, SUVmean, TLG."
    ),
    "pet_ct_brain": (
        "Track SUVR and centiloid scores. Amyloid progression threshold: centiloid ≥50 = elevated. "
        "Tau: region-specific SUVR changes of >0.1/year are clinically significant. "
        "Flag cognitive-region asymmetry changes."
    ),
}

_PROMPT_TEMPLATE = """\
You are a specialist radiologist AI performing longitudinal analysis on serial {usecase_label} studies for the same patient.

CLINICAL GUIDELINES:
{guidance}

CURRENT STUDY (most recent):
Measurements: {current_measurements}
Summary: {current_summary}

PRIOR STUDIES (chronological, oldest first):
{prior_block}

TASK: Compare the current study against the prior studies. Identify the overall trend, compute key metric changes relative to the earliest available timepoint (baseline), and provide a clinical interpretation.

Respond ONLY with a valid JSON object — no markdown fences, no extra text — using this exact schema:
{{
  "trend": "<progression|regression|stable|mixed|insufficient_data>",
  "response_category": "<CR|PR|SD|PD|not_applicable>",
  "key_changes": [
    {{
      "metric": "<metric name>",
      "baseline_value": <number or null>,
      "current_value": <number or null>,
      "change_pct": <number or null>,
      "direction": "<increased|decreased|stable>"
    }}
  ],
  "clinical_significance": "<2-3 sentence interpretation of the longitudinal trend>",
  "follow_up_recommendation": "<recommended follow-up action and timeframe>",
  "timespan_days": <integer — days from earliest prior to current>,
  "studies_compared": <integer — total studies including current>,
  "disclaimer": "AI-generated longitudinal analysis — requires radiologist verification before clinical use."
}}

Valid trend values: progression, regression, stable, mixed, insufficient_data
Valid response_category values: CR, PR, SD, PD, not_applicable
"""


class LongitudinalAnalysisService:
    """Phase 4: LLM-driven longitudinal trend analysis across serial studies."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def available(self) -> bool:
        return getattr(self._client, "_ready", False)

    async def analyze(
        self,
        usecase_name: str,
        current_measurements: dict[str, Any],
        current_summary: dict[str, Any],
        prior_timepoints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate longitudinal trend analysis.

        prior_timepoints: list of dicts with keys measurements, summary, created_at (ISO str).
        Returns empty dict if fewer than 1 prior timepoint or on any failure.
        """
        if not self.available:
            logger.warning("longitudinal_service_unavailable")
            return {}
        if not prior_timepoints:
            return {"trend": "insufficient_data", "studies_compared": 1,
                    "clinical_significance": "No prior studies available for comparison.",
                    "disclaimer": "AI-generated longitudinal analysis — requires radiologist verification before clinical use."}

        try:
            prompt = self._build_prompt(
                usecase_name, current_measurements, current_summary, prior_timepoints
            )
            raw = await self._client.generate_text(prompt)
            return self._parse_and_validate(raw, len(prior_timepoints) + 1)
        except Exception as exc:
            logger.warning("longitudinal_analysis_failed", error=str(exc))
            return {}

    # ── private ──────────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        usecase_name: str,
        current_measurements: dict,
        current_summary: dict,
        prior_timepoints: list[dict],
    ) -> str:
        guidance = _USECASE_GUIDANCE.get(
            usecase_name,
            "Apply standard radiological criteria for this modality and use case.",
        )
        usecase_label = usecase_name.replace("_", " ").title()

        # Strip longitudinal_analysis and clinical_context from summaries to avoid circularity
        def _clean_summary(s: dict) -> dict:
            return {k: v for k, v in s.items()
                    if k not in ("clinical_context", "longitudinal_analysis")}

        prior_lines: list[str] = []
        for i, tp in enumerate(prior_timepoints, start=1):
            created = tp.get("created_at", "unknown date")
            meas = tp.get("measurements", {})
            summ = _clean_summary(tp.get("summary", {}))
            prior_lines.append(
                f"  Prior study {i} (acquired {created}):\n"
                f"    Measurements: {json.dumps(meas, default=str)}\n"
                f"    Summary: {json.dumps(summ, default=str)}"
            )

        return _PROMPT_TEMPLATE.format(
            usecase_label=usecase_label,
            guidance=guidance,
            current_measurements=json.dumps(current_measurements, default=str),
            current_summary=json.dumps(_clean_summary(current_summary), default=str),
            prior_block="\n".join(prior_lines) if prior_lines else "  (none)",
        )

    def _parse_and_validate(self, raw: str, studies_compared: int) -> dict[str, Any]:
        text = raw.strip()
        data: dict[str, Any] | None = None
        for attempt in (text, re.sub(r"```(?:json)?", "", text).replace("```", "").strip()):
            try:
                data = json.loads(attempt)
                break
            except json.JSONDecodeError:
                pass
        if data is None:
            # Scan for first parseable JSON object (string-aware, handles thinking output)
            cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
            decoder = json.JSONDecoder()
            idx = 0
            while data is None:
                start = cleaned.find("{", idx)
                if start == -1:
                    break
                try:
                    data, _ = decoder.raw_decode(cleaned, start)
                except json.JSONDecodeError:
                    idx = start + 1
        if data is None:
            logger.warning("longitudinal_json_parse_failed", raw=raw[:300])
            return {}

        # Normalise and clamp enum fields
        trend = str(data.get("trend", "insufficient_data")).lower().replace(" ", "_")
        if trend not in _VALID_TRENDS:
            trend = "insufficient_data"
        data["trend"] = trend

        response_cat = str(data.get("response_category", "not_applicable")).upper()
        if response_cat not in _VALID_RESPONSES:
            response_cat = "not_applicable"
        data["response_category"] = response_cat

        # Ensure studies_compared is always set
        data.setdefault("studies_compared", studies_compared)

        # Ensure disclaimer is always present
        data.setdefault(
            "disclaimer",
            "AI-generated longitudinal analysis — requires radiologist verification before clinical use.",
        )

        # Ensure key_changes is a list
        if not isinstance(data.get("key_changes"), list):
            data["key_changes"] = []

        return data
