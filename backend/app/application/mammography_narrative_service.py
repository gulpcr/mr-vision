from __future__ import annotations

"""Compose per-breast mammography narrative (findings prose + opinion) FROM the
structured finding slots.

The structured slots (density / mass / calcification / skin thickening / nipple
retraction / architectural distortion / axillary nodes) are the ground truth. This
service renders them into report prose — first deterministically (template strings),
then optionally polished by Gemini. The Gemini prompt is constrained to *rephrase only*
the listed facts; it must never add, infer, or omit a finding. On any failure or when
the LLM is disabled, the deterministic text is used.
"""

from typing import Any

import structlog

from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

_SIDES = {"right": "right", "left": "left"}

_DENSITY_TEXT = {
    "a": "The breast is almost entirely fatty",
    "b": "There are scattered areas of fibroglandular density",
    "c": "The breast tissue is heterogeneously dense",
    "d": "The breast tissue is extremely dense",
}

_POLISH_PROMPT = """\
Rewrite the following factual mammography findings for the {side} breast into 2-4 sentences \
of professional radiology-report prose. Do NOT add, infer, remove, or change any finding — \
only rephrase exactly what is listed. Output the prose only, no preamble.

Facts:
{facts}
"""


class MammographyNarrativeService:
    """Renders structured mammography slots into narrative findings + opinion."""

    def __init__(self, client: GeminiClient | None = None):
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._client and self._client.ready)

    async def generate(
        self,
        findings: dict[str, Any],
        laterality: str,
        birads_right: int | None,
        birads_left: int | None,
    ) -> dict[str, str | None]:
        """Return {"right_breast_findings", "left_breast_findings", "opinion"}.
        A side with no slots (not imaged) yields None for that side."""
        right_slots = (findings or {}).get("right")
        left_slots = (findings or {}).get("left")

        right_text = self._deterministic_side("right", right_slots) if right_slots else None
        left_text = self._deterministic_side("left", left_slots) if left_slots else None
        opinion = self._deterministic_opinion(
            right_slots, left_slots, birads_right, birads_left, laterality
        )

        # Optional LLM polish of the per-breast prose (facts unchanged).
        if self.available:
            right_text = await self._polish("right", right_slots, right_text)
            left_text = await self._polish("left", left_slots, left_text)

        return {
            "right_breast_findings": right_text,
            "left_breast_findings": left_text,
            "opinion": opinion,
        }

    # ── Deterministic rendering (ground truth / fallback) ───────────────────────

    @staticmethod
    def _side_sentences(slots: dict[str, Any]) -> list[str]:
        s: list[str] = []
        location = slots.get("location", {}) or {}
        density = slots.get("density")
        if density in _DENSITY_TEXT:
            s.append(_DENSITY_TEXT[density] + ".")

        if slots.get("mass") == "present":
            loc = f" in the {location['mass']} aspect of the breast" if location.get("mass") else ""
            s.append(
                f"A soft tissue mass is detected{loc} (AI-localized; radiologist to confirm "
                "exact quadrant and characterize)."
            )
        elif slots.get("mass") == "none":
            s.append("There is no evidence of a stellate or spiculated mass.")

        if slots.get("calcification") == "present":
            loc = f" in the {location['calcification']} aspect" if location.get("calcification") else ""
            s.append(f"Suspicious clustered microcalcifications are detected{loc} (AI-localized).")
        elif slots.get("calcification") == "none":
            s.append("There is no evidence of clustered microcalcification.")

        if slots.get("architectural_distortion") == "present":
            s.append("Architectural distortion is noted.")
        elif slots.get("architectural_distortion") == "none":
            s.append("There is no evidence of trabecular distortion.")

        if slots.get("skin_thickening") == "present":
            s.append("Skin thickening is noted.")
        elif slots.get("skin_thickening") == "none":
            s.append("There is no skin thickening.")

        if slots.get("nipple_retraction") == "present":
            s.append("Nipple retraction is noted.")
        elif slots.get("nipple_retraction") == "none":
            s.append("There is no nipple retraction.")

        nodes = slots.get("axillary_nodes")
        if nodes == "abnormal":
            s.append("Abnormal axillary lymph nodes are noted.")
        elif nodes == "normal":
            s.append("Lymph nodes with intact hila are seen in the axilla.")
        return s

    def _deterministic_side(self, side: str, slots: dict[str, Any]) -> str:
        sentences = self._side_sentences(slots)
        if not sentences:
            return f"The {side} breast was not assessable from the available views."
        return " ".join(sentences)

    def _deterministic_opinion(
        self,
        right_slots: dict[str, Any] | None,
        left_slots: dict[str, Any] | None,
        birads_right: int | None,
        birads_left: int | None,
        laterality: str,
    ) -> str:
        def _has_positive(slots: dict[str, Any] | None) -> bool:
            if not slots:
                return False
            return (
                slots.get("mass") == "present"
                or slots.get("calcification") == "present"
                or slots.get("architectural_distortion") == "present"
            )

        parts: list[str] = []
        if right_slots is not None:
            flag = " with AI-flagged finding(s)" if _has_positive(right_slots) else ""
            br = f" (AI-suggested BI-RADS {birads_right})" if birads_right is not None else ""
            parts.append(f"Right breast{flag}{br}")
        if left_slots is not None:
            flag = " with AI-flagged finding(s)" if _has_positive(left_slots) else ""
            bl = f" (AI-suggested BI-RADS {birads_left})" if birads_left is not None else ""
            parts.append(f"Left breast{flag}{bl}")
        body = ". ".join(parts) if parts else "No breast was assessable"
        return (
            body
            + ". These are AI suggestions from structured detectors and are NON-DIAGNOSTIC; "
            "the reporting radiologist confirms all findings and assigns the final BI-RADS. "
            "Advise ultrasound correlation as clinically indicated."
        )

    # ── Optional LLM polish (facts constrained to the slots) ────────────────────

    async def _polish(self, side: str, slots: dict[str, Any] | None, fallback: str | None) -> str | None:
        if not slots or not fallback:
            return fallback
        facts = "\n".join(f"- {line}" for line in self._side_sentences(slots))
        if not facts:
            return fallback
        try:
            prose = await self._client.generate_text(
                _POLISH_PROMPT.format(side=side, facts=facts)
            )
            return prose.strip() or fallback
        except Exception as exc:
            logger.warning("mammo_narrative_polish_failed", side=side, error=str(exc))
            return fallback
