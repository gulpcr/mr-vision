from __future__ import annotations

"""AI radiologist for bilateral mammography: Gemini reads the actual rendered views TOGETHER
with the fine-tuned model's per-breast finding probabilities and the clinical context, then
writes the report the way a breast radiologist would — the AECH-KIRAN layout: per-breast
narrative findings, a per-breast opinion with recommendation, and a per-breast BI-RADS category.

This is deliberately different from ``MammographyNarrativeService`` (which only rephrases the
structured slots and may not add/infer anything). Here Gemini is asked to UNDERSTAND the case and
INFER findings from the images, using the model's probabilities as grounding evidence — not as a
hard ceiling. On any failure / unavailability / malformed response it returns None so the caller
falls back to the deterministic narrative service. NON-DIAGNOSTIC; the radiologist confirms.
"""

import json
import re
from typing import Any

import structlog

from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

_DEFAULT_DISCLAIMER = (
    "AI-generated mammography report — requires radiologist verification before clinical use."
)

# Report slot -> human phrase, for grounding the model evidence in the prompt.
_SLOT_PHRASE = {
    "density": "breast density (a-d)",
    "mass": "mass",
    "calcification": "clustered microcalcification",
    "architectural_distortion": "architectural distortion",
    "skin_thickening": "skin thickening",
    "nipple_retraction": "nipple retraction",
    "axillary_nodes": "axillary lymph nodes",
}

_PROMPT_TEMPLATE = """\
You are a senior consultant breast radiologist producing the FINAL written report for a bilateral \
digital mammogram (standard CC and MLO views). You are given the rendered mammogram images for \
this study, followed by an AI model's per-breast finding probabilities and the clinical context. \
Write a thorough, profound, publication-quality report — the definitive read, not a draft.
{clinical_block}
The AI model below is a VinDr-trained classifier. Treat its probabilities as SUPPORTING EVIDENCE \
and a floor for what to look for — not as unquestionable truth. You MUST READ THE IMAGES YOURSELF \
and describe what you actually see, correlating it with the model evidence and the clinical \
history. The model's rare-finding heads (architectural distortion, skin thickening, nipple \
retraction, axillary nodes) were trained on very few cases and are unreliable — weight the images \
and clinical context far more heavily for those, and do not assert one of them solely because the \
model flagged it.

CRITICAL — DO NOT FABRICATE QUANTITATIVE DETAIL. You are reading a downsized rendered image with \
no scale bar, no calibration, no laterality/orientation markers, and no prior study. You therefore \
CANNOT measure anything. Do NOT state any precise measurement or coordinate you cannot actually \
derive: no lesion size in cm/mm, no clock-face position (e.g. "10 o'clock"), no distance from the \
nipple, no exact count of calcifications, and no comparison to a non-existent prior. Describe \
location QUALITATIVELY by region only (e.g. "upper-outer quadrant", "retroareolar", "superolateral", \
"central") and size qualitatively at most (e.g. "small", "large", "dominant") — never a number. If \
a specific measurement or position would normally be reported, explicitly defer it (e.g. "size and \
precise location to be confirmed on dedicated diagnostic views / ultrasound"). Inventing a number \
is a serious error; omitting or deferring it is correct.

Model evidence per breast (probability that each finding is PRESENT; density is the model's \
BI-RADS composition call):
{evidence_block}
{birads_block}
CRITICAL — THE REPORT MUST READ AS YOUR OWN RADIOLOGIST INTERPRETATION. The model evidence and the \
pre-assigned BI-RADS above are INTERNAL grounding for your read only. Do NOT expose them in the \
report. Specifically, in the findings, opinion and clinical_features you must NOT: mention "the AI \
model", "the model", "the classifier", "AI-suggested", "AI assessment", or that any automated tool \
produced a finding; print ANY numeric confidence, probability or score (e.g. "(confidence 0.82)", \
"probability 0.1"); or phrase a finding as agreement/consistency with a model (e.g. "consistent \
with the model's assessment of mass=none"). State every finding DIRECTLY as your own observation \
(e.g. "No suspicious mass is identified", "The parenchyma is heterogeneously dense (ACR category \
c)"). No number that is a confidence, probability or score may appear anywhere in the output.

Write in the ACR BI-RADS lexicon, in flowing professional radiology prose (never a checklist). \
Be comprehensive and specific. For EACH imaged breast, the "findings" must systematically address \
ALL of the following, in this order, describing what is seen (and explicitly stating the negative \
when a feature is absent — e.g. "There is no evidence of a stellate or spiculated mass"):
  1. Breast composition / parenchymal density (ACR category a-d, in words).
  2. Masses — presence/absence; if present, describe location by QUADRANT/REGION only (never a \
clock position or a distance from the nipple), qualitative size only (never cm/mm), shape \
(oval/round/irregular), margins (circumscribed/obscured/microlobulated/indistinct/spiculated), \
and density — defer exact size/position to diagnostic views/ultrasound.
  3. Calcifications — presence/absence; if present, morphology (e.g. amorphous, coarse \
heterogeneous, fine pleomorphic, fine linear branching) and distribution qualitatively \
(grouped/clustered, linear, segmental, regional, diffuse) — do NOT state an exact number of \
calcifications.
  4. Architectural / trabecular distortion.
  5. Asymmetries (asymmetry, focal, global, developing) if any.
  6. Associated features — skin thickening, skin/nipple retraction, trabecular thickening.
  7. Axilla — lymph nodes (e.g. "lymph nodes with preserved fatty hila" when normal; describe \
enlargement / loss of hilum / cortical thickening if abnormal).
Correlate with the clinical history throughout: e.g. for a post-lumpectomy breast, describe \
expected post-surgical change (architectural distortion, skin thickening/retraction and surgical \
clips at the operative bed) and explicitly state whether residual/recurrent disease can or cannot \
be excluded on mammography alone.

Then write a comprehensive "opinion" (impression) that: synthesises the salient findings per \
breast; gives an explicit, actionable recommendation (e.g. "Advise ultrasound correlation", \
"correlate with prior mammograms", "tissue sampling advised" — as appropriate); and states the \
pre-assigned BI-RADS category per breast in the prose.

BI-RADS CATEGORY — REPORT AS ASSIGNED, DO NOT OVERRIDE. The BI-RADS category for each imaged \
breast has already been assigned by the platform (shown above) and is authoritative. You MUST use \
that exact category in your prose and echo it unchanged in the JSON below — do NOT substitute your \
own number, even if your read of the images would suggest a different category. Your narrative must \
be consistent with the assigned category. If the images make you doubt the assigned category, say \
so qualitatively in the opinion (e.g. "features warrant careful correlation") and recommend the \
appropriate work-up — but still report the assigned BI-RADS number. Reference (for wording only): \
0 = incomplete, needs further imaging; 1 = negative; 2 = benign; 3 = probably benign; \
4 = suspicious; 5 = highly suggestive of malignancy; 6 = biopsy-proven malignancy. If a breast has \
no assigned category above, set its birads to null.

Summarise the clinical history into a concise "clinical_features" string.
Only report on a breast that was imaged (present in the evidence below). If a breast is absent from \
the evidence, set its findings to null and its birads to null.

Respond ONLY with a valid JSON object — no markdown fences, no extra text. The findings strings \
should be full multi-sentence paragraphs:
{{
  "clinical_features": "<concise clinical summary, or null>",
  "right_breast_findings": "<comprehensive multi-sentence paragraph, or null if not imaged>",
  "left_breast_findings": "<comprehensive multi-sentence paragraph, or null if not imaged>",
  "opinion": "<comprehensive impression covering each imaged breast, with BI-RADS and recommendations>",
  "birads_right": <integer 0-6 or null>,
  "birads_left": <integer 0-6 or null>,
  "disclaimer": "{disclaimer}"
}}
"""


class MammographyRadiologistService:
    """Generates the mammography report (findings/opinion/BI-RADS) via Gemini Vision."""

    def __init__(self, client: GeminiClient | None):
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._client and self._client.ready)

    async def generate(
        self,
        findings: dict[str, Any],
        laterality: str,
        images: list[bytes],
        clinical_indication: str | None = None,
        clinical_history: str | None = None,
        qa_flags: list[str] | None = None,
        patient_age: str | None = None,
        patient_sex: str | None = None,
        birads_right: int | None = None,
        birads_left: int | None = None,
    ) -> dict[str, Any] | None:
        """Returns {clinical_features, right_breast_findings, left_breast_findings, opinion,
        birads_right, birads_left, disclaimer} or None on failure/unavailability.

        ``birads_right``/``birads_left`` are the platform's pre-assigned BI-RADS categories.
        They are authoritative: the model is instructed to report them as-is (in prose and JSON)
        and NOT to substitute its own — the caller also ignores any model-returned BI-RADS."""
        if not self.available or not images:
            return None
        prompt = _build_prompt(
            findings, laterality, clinical_indication, clinical_history,
            qa_flags, patient_age, patient_sex, birads_right, birads_left,
        )
        try:
            raw = await self._client.generate_from_images(prompt, images)
            parsed = _parse_json_response(raw)
            validated = _validate(parsed)
            if validated is None:
                logger.warning("mammo_radiologist_invalid_response", raw_preview=raw[:300])
            return validated
        except Exception as exc:
            logger.warning("mammo_radiologist_generation_failed", error=str(exc))
            return None


# ── Internal helpers ────────────────────────────────────────────────────────────


def _evidence_for_side(label: str, slots: dict[str, Any] | None) -> str:
    if not slots:
        return f"  {label}: not imaged."
    conf = slots.get("confidence", {}) or {}
    low = set(slots.get("low_confidence", []) or [])
    parts = []
    for slot, phrase in _SLOT_PHRASE.items():
        val = slots.get(slot)
        if val is None:
            continue
        c = conf.get(slot)
        tag = " [low-confidence, confirm from image]" if slot in low else ""
        cstr = f" (conf {c})" if c is not None else ""
        parts.append(f"{phrase}={val}{cstr}{tag}")
    return f"  {label}: " + "; ".join(parts) if parts else f"  {label}: no model calls."


def _birads_block(birads_right: int | None, birads_left: int | None) -> str:
    """Pre-assigned, authoritative BI-RADS categories for the prompt (report-as-is)."""
    def _fmt(side: str, val: int | None) -> str:
        return f"  {side} breast: BI-RADS {val}" if val is not None else f"  {side} breast: not assigned"
    return (
        "Pre-assigned BI-RADS category per breast (AUTHORITATIVE — report exactly this, do not "
        "override):\n"
        + _fmt("RIGHT", birads_right) + "\n"
        + _fmt("LEFT", birads_left)
    )


def _build_prompt(
    findings: dict[str, Any],
    laterality: str,
    clinical_indication: str | None,
    clinical_history: str | None,
    qa_flags: list[str] | None,
    patient_age: str | None,
    patient_sex: str | None,
    birads_right: int | None = None,
    birads_left: int | None = None,
) -> str:
    clinical_lines = []
    if patient_age:
        clinical_lines.append(f"  - Age: {patient_age}")
    if patient_sex:
        clinical_lines.append(f"  - Sex: {patient_sex}")
    if clinical_indication:
        clinical_lines.append(f"  - Reason for study: {clinical_indication}")
    if clinical_history:
        clinical_lines.append(f"  - Clinical history: {clinical_history}")
    if qa_flags:
        clinical_lines.append("  - Technical QA caveats: " + ", ".join(f.replace("_", " ") for f in qa_flags))
    clinical_block = (
        "\nClinical context:\n" + "\n".join(clinical_lines) + "\n" if clinical_lines else "\n"
    )

    evidence_block = (
        _evidence_for_side("RIGHT breast", (findings or {}).get("right")) + "\n"
        + _evidence_for_side("LEFT breast", (findings or {}).get("left"))
    )

    return _PROMPT_TEMPLATE.format(
        clinical_block=clinical_block,
        evidence_block=evidence_block,
        birads_block=_birads_block(birads_right, birads_left),
        disclaimer=_DEFAULT_DISCLAIMER,
    )


def _parse_json_response(text: str) -> dict[str, Any]:
    """Parse JSON from a Gemini response, tolerating fences / surrounding prose."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
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


def _birads(val: Any) -> int | None:
    try:
        i = int(val)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i <= 6 else None


def _validate(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise; require at least one breast's findings + an opinion, else None (fallback)."""
    if not isinstance(raw, dict):
        return None

    def _text(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    right = _text(raw.get("right_breast_findings"))
    left = _text(raw.get("left_breast_findings"))
    opinion = _text(raw.get("opinion"))
    if not (right or left) or not opinion:
        return None

    disclaimer = _text(raw.get("disclaimer")) or _DEFAULT_DISCLAIMER
    return {
        "clinical_features": _text(raw.get("clinical_features")),
        "right_breast_findings": right,
        "left_breast_findings": left,
        "opinion": opinion,
        "birads_right": _birads(raw.get("birads_right")),
        "birads_left": _birads(raw.get("birads_left")),
        "disclaimer": disclaimer,
    }
