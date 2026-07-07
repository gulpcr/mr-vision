from __future__ import annotations

"""Generate the coronary_cta report's narrative by having Gemini act as an autonomous
cardiovascular radiologist: it reads the calcium-overlay/stenosis-crop images AND the
pipeline's computed numbers, and writes its own clinical report in Markdown prose.

Design philosophy (deliberately changed from a stricter transcription-only version this
service originally shipped with): the pipeline's Agatston/stenosis/territory/plaque numbers
are presented to the model as "Automated Pipeline Insights" — preliminary, mechanically
computed hints, not verdicts the model must copy verbatim. The model has explicit clinical
authority to weigh those hints against what it sees in the images and reach its own
conclusions, including ones that diverge from a given number (e.g. calcium blooming making a
geometric stenosis estimate look conservative, or a visually LAD-like course for a vessel the
geometric heuristic left "unassigned"). See ``_PROMPT_TEMPLATE`` for the exact protocol.

This is a real safety trade-off, not a free upgrade: letting a VLM revise a deterministic CT
measurement from a 2D crop reintroduces the kind of visual-estimation hallucination risk the
original "numbers are ground truth" design existed specifically to avoid. Two things are kept
regardless of that trade-off because they don't conflict with model autonomy: (1) the prompt
requires the model to always STATE the pipeline's raw number alongside any overridden
impression, so a reviewing radiologist can always see what the automated tier actually
measured; (2) the output must still carry the "requires radiologist verification" disclaimer.

Output is free-form Markdown, not a structured/validated object — there is no per-field
grounding check anymore (see ``_sanity_check_report`` for what IS still checked: the response
isn't empty, isn't a refusal, isn't still raw JSON, and carries the disclaimer). On any
failure, unavailability, or failed sanity check, ``generate()`` returns ``None`` — the caller
falls back to the deterministic ``summary["diagnosis"]``/``summary["processing_notes"]`` that
``postprocess()`` always sets regardless of whether this service ever runs.
"""

import re
from typing import Any

import structlog
from pydantic import ValidationError

from app.application.coronary_cta_narrative_schemas import (
    CadRadsAssessment,
    CoronaryCtaFindings,
    QuantitativeScores,
    StudyMetadata,
    VesselFinding,
)
from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

_DEFAULT_DISCLAIMER = (
    "AI-generated radiology findings — requires radiologist verification before clinical use."
)

_QA_FLAG_DESCRIPTIONS = {
    "no_calcium_series": "No non-contrast calcium-score series was available for this study.",
    "no_ccta_series": "No contrast CCTA series was available; stenosis grading was not possible.",
    "calcium_slice_thickness_abnormal": (
        "The calcium-score series slice thickness is outside the standard range."
    ),
    "insufficient_coverage": (
        "The calcium-score series has unusually few slices; anatomic coverage may be incomplete."
    ),
    "calcium_roi_approximate": (
        "Calcium detection used a heuristic cardiac bounding box, not a learned heart mask — "
        "the score may include some non-coronary calcium."
    ),
    "stenosis_segment_labels_approximate": (
        "Vessel segments are geometric branches only; they are NOT mapped to named coronary "
        "anatomy by the pipeline itself (LAD/LCx/RCA/left main)."
    ),
    "coronary_lumen_unavailable": (
        "Coronary lumen segmentation failed or was unavailable; stenosis was not assessed."
    ),
    "vessel_territory_unavailable": (
        "Vessel-territory chamber segmentation failed or was unavailable; the pipeline offers "
        "no anatomical territory guess for any vessel."
    ),
    "vessel_territory_heuristic_unvalidated": (
        "Any pipeline vessel_territory label (LM/LAD/LCx/RCA) comes from an unvalidated "
        "geometric heuristic, not a registered anatomical mapping."
    ),
    "plaque_type_heuristic_hu_based": (
        "Any pipeline plaque_type comes from a deterministic HU-threshold heuristic on the "
        "vessel wall, not a learned plaque classifier."
    ),
}

_PROMPT_TEMPLATE = """\
You are an autonomous, board-certified cardiovascular radiologist reading a Coronary CT study \
end-to-end. An automated pipeline has pre-processed this study and surfaced a set of \
preliminary measurements, but YOU are the diagnostician with full clinical authority over the \
final read. Weigh the pipeline's numbers against what you actually see in the attached images, \
and write the report your own clinical judgment supports.

## What the pipeline gives you (preliminary hints, not verdicts)
The JSON block below is labeled "Automated Pipeline Insights". Treat every value in it the way \
you would treat a resident's pre-read or CAD-software flag: a fast, mechanically computed \
starting point that is often right, sometimes conservative, and occasionally wrong — never \
something to transcribe uncritically, and never something you're forbidden from questioning.
  - The Agatston score, calcium volume, and lesion count come from deterministic HU-threshold \
scoring on the non-contrast series — generally reliable arithmetic, but check the attached \
calcium-overlay image(s) against it: if the highlighted calcium looks sparser or more extensive \
than the reported score would suggest, say so.
  - Each vessel's stenosis %, reference/minimal diameter, and grade come from a purely \
geometric (skeleton + distance-transform) measurement on an automatically segmented lumen — \
this method cannot reason about calcium blooming, motion artifact, or diffuse disease the way \
you can from the image.
  - vessel_territory (LM/LAD/LCx/RCA) and plaque_type, when present, come from separate, \
unvalidated geometric/HU heuristics layered on top of the stenosis grading — useful hints, not \
conclusions.

## Your protocol when the pipeline and your own read disagree
You will sometimes see something the numbers don't fully capture. Handle it the way you would \
dictate a real discrepancy to a colleague — state it plainly, don't silently pick a side:
  - If a vessel's stenosis crop shows dense calcification with blooming that could make the \
true lumen narrower or wider than the geometric estimate suggests, state the pipeline's number, \
state your own visual impression, and give your final assessment.
  - If a vessel's territory is "unassigned" but its position and course in the image look \
visually consistent with a specific vessel (e.g. running along the anterior interventricular \
groove like a typical LAD), you MAY offer that as your own visual impression — clearly framed \
as your read (e.g. "visually most consistent with the LAD territory"), not stated as confirmed \
anatomy from a registered map.
  - If plaque_type is absent for a vessel, characterize its plaque directly from the image \
(density, distribution, calcified vs. soft) as you would on any CCTA read — you are not limited \
to what the heuristic computed.
  - If the pipeline's numbers and your visual read are concordant, say so briefly and move on — \
you don't need to manufacture a disagreement.
  - Whatever your final impression, always state the pipeline's raw number for it at least once \
(e.g. "pipeline-estimated stenosis 68%, visually appears more severe given dense calcific \
blooming") — this keeps your report auditable against the automated tier even where you differ \
from it.
  - Never invent a vessel, a calcium focus, or a finding with no basis in either the pipeline \
data or the images actually provided. Autonomy means independent clinical reasoning about what \
is in front of you — not fabrication of things that aren't.
  - This study's images are tightly cropped to the heart/coronary field of view. You cannot \
assess extracardiac structures (e.g. lung) from them — state that incidental findings were not \
assessed outside this field of view rather than commenting on anatomy you cannot see.

## Automated Pipeline Insights (preliminary hints)
```json
{findings_json}
```

## Known pipeline caveats for this study
Weave these naturally into your Technique and Coronary Arteries findings wherever relevant — \
do not list them verbatim as a bullet dump:
{qa_block}

## Images provided
{image_legend}

## Referring clinical context
{clinical_block}

## Report format
Write a complete, standard cardiovascular radiology report in clean Markdown prose — headings, \
short paragraphs, and bullet lists where a real report would use them. Do not wrap your answer \
in a code fence and do not output JSON — this is a document a radiologist will read, not data \
a program will parse. Use this structure, adapting section content to what's actually present \
(do not pad a section that has nothing to say):

# Coronary CTA Report

**Clinical Indication:** ...

## Technique
...

## Findings

### Coronary Calcium Scoring
...

### Coronary Arteries
...

### Incidental Findings
...

## Impression
- ...

## Recommendation
...

---
*{disclaimer}*
"""


class CoronaryCtaNarrativeService:
    """Generates the coronary_cta report's autonomous Markdown narrative via Gemini Vision."""

    def __init__(self, client: GeminiClient | None):
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._client and self._client.ready)

    async def generate(
        self,
        summary: dict[str, Any],
        measurements: dict[str, Any],
        images: dict[str, bytes],
        qa_flags: list[str] | None = None,
        clinical_indication: str | None = None,
        clinical_history: str | None = None,
    ) -> dict[str, Any] | None:
        """Returns ``{"markdown_report": <str>}``, or ``None`` on failure/unavailability/a
        failed sanity check — callers must fall back to the deterministic
        ``diagnosis``/``processing_notes`` already set by ``postprocess()``.

        ``images`` should be the calcium-overlay and/or stenosis-crop PNGs from
        ``postprocessed["artifacts"]`` (``artifact_type in ("overlay_png", "stenosis_crop_png")``).
        """
        if not self.available or not images:
            return None

        qa_flags = qa_flags or []
        try:
            findings = build_findings_payload(summary, measurements, qa_flags)
        except ValidationError as exc:
            logger.warning("coronary_cta_findings_payload_invalid", error=str(exc))
            return None

        prompt = _build_prompt(findings, images, clinical_indication, clinical_history)
        try:
            raw = await self._client.generate_markdown_from_images(prompt, list(images.values()))
        except Exception as exc:
            logger.warning("coronary_cta_narrative_generation_failed", error=str(exc))
            return None

        report_text = _clean_markdown_response(raw)
        problems = _sanity_check_report(report_text)
        if problems:
            logger.warning(
                "coronary_cta_narrative_sanity_check_failed",
                problems=problems, raw_preview=raw[:300],
            )
            return None

        soft_warnings = _soft_traceability_check(report_text, findings)
        if soft_warnings:
            # Logged only, never rejected — the model has explicit authority to reach its
            # own conclusion; this just flags when the raw pipeline numbers it was asked to
            # always cite don't show up verbatim, for monitoring/prompt-tuning purposes.
            logger.info("coronary_cta_narrative_traceability_warning", warnings=soft_warnings)

        return {"markdown_report": report_text}


# ── Internal helpers ──────────────────────────────────────────────────────────


def build_findings_payload(
    summary: dict[str, Any], measurements: dict[str, Any], qa_flags: list[str]
) -> CoronaryCtaFindings:
    """Adapts coronary_cta's ``postprocess()`` dict output into the validated "Automated
    Pipeline Insights" contract. Raises ``pydantic.ValidationError`` if the pipeline output
    doesn't match the expected shape/ranges — treated by the caller as "do not attempt
    narrative generation", never coerced or patched silently."""
    segments = measurements.get("segments", []) or []
    vessel_findings = [
        VesselFinding(
            vessel_label=seg["name"],
            vessel_territory=seg.get("vessel") or "unassigned",
            vessel_confidence=seg.get("vessel_confidence"),
            stenosis_percentage=seg["stenosis_pct"],
            stenosis_grade=seg["grade"],
            reference_diameter_mm=seg["reference_diameter_mm"],
            minimal_lumen_diameter_mm=seg["min_lumen_diameter_mm"],
            centerline_length_mm=seg["centerline_length_mm"],
            plaque_type=seg.get("plaque_type"),
        )
        for seg in segments
    ]

    return CoronaryCtaFindings(
        study_metadata=StudyMetadata(
            inference_method=summary.get("inference_method", "calcium_only"),
            image_dimensions=list(measurements.get("image_dimensions", []) or []),
            voxel_spacing_mm=list(measurements.get("voxel_spacing_mm", []) or []),
        ),
        quantitative_scores=QuantitativeScores(
            agatston_calcium_score=summary.get("calcium_score_agatston", 0.0),
            calcium_category=summary.get("calcium_category", "Zero (no detectable calcium)"),
            calcium_volume_mm3=summary.get("calcium_volume_mm3", 0.0),
            calcium_lesion_count=summary.get("calcium_lesion_count", 0),
            calcium_score_valid="no_calcium_series" not in qa_flags,
        ),
        cad_rads_assessment=CadRadsAssessment(
            stenosis_analysis_available=summary.get("stenosis_analysis_available", False),
            max_stenosis_percentage=summary.get("max_stenosis_pct"),
            cad_rads_category=summary.get("cad_rads"),
        ),
        vessel_findings=vessel_findings,
        qa_flags=list(qa_flags),
        processing_notes=summary.get("processing_notes", ""),
    )


def _build_prompt(
    findings: CoronaryCtaFindings,
    images: dict[str, bytes],
    clinical_indication: str | None,
    clinical_history: str | None,
) -> str:
    findings_json = findings.model_dump_json(indent=2)

    qa_lines = [
        f"  - {_QA_FLAG_DESCRIPTIONS.get(flag, flag.replace('_', ' '))}"
        for flag in findings.qa_flags
    ]
    qa_block = "\n".join(qa_lines) if qa_lines else "  - None."

    image_names = sorted(images.keys())
    legend_lines = []
    for i, name in enumerate(image_names):
        if name.startswith("stenosis_crop_"):
            legend_lines.append(
                f"  {i + 1}. {name} — contrast CCTA, a tight axial crop centered on one "
                "graded vessel's narrowest point per the pipeline's geometric estimate, "
                "titled with that vessel's label and its estimated stenosis %. Form your own "
                "visual impression of the narrowing, calcium blooming, and plaque appearance "
                "— you may agree with, refine, or note disagreement with the pipeline's "
                "number, but always state the pipeline's number too (see protocol above)."
            )
        else:
            legend_lines.append(
                f"  {i + 1}. {name} — non-contrast calcium-score CT, an axial slice ranked "
                "by calcium burden (rank 0 = highest), with detected calcium highlighted in "
                "an overlay color. Sanity-check the pipeline's calcium detection against what "
                "you actually see, but always state the pipeline's Agatston score as the "
                "quantitative reference."
            )
    image_legend = (
        "\n".join(legend_lines)
        if legend_lines
        else "  (none provided — rely solely on the structured findings above)"
    )

    clinical_lines = []
    if clinical_indication:
        clinical_lines.append(f"  - Reason for study: {clinical_indication}")
    if clinical_history:
        clinical_lines.append(f"  - Clinical history: {clinical_history}")
    clinical_block = "\n".join(clinical_lines) if clinical_lines else "  - Not provided."

    return _PROMPT_TEMPLATE.format(
        findings_json=findings_json,
        qa_block=qa_block,
        image_legend=image_legend,
        clinical_block=clinical_block,
        disclaimer=_DEFAULT_DISCLAIMER,
    )


def _clean_markdown_response(text: str) -> str:
    """Defensive fence-stripping in case the model wraps its answer in a code fence despite
    being told not to — does not otherwise alter the model's wording."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


def _sanity_check_report(text: str) -> list[str]:
    """Coarse structural safety checks — deliberately NOT number-by-number grounding. The
    model has explicit clinical authority to reconcile pipeline hints against the images (see
    the module docstring), so this only rejects responses that are empty, refusals, still raw
    JSON despite instructions, or missing the mandatory disclaimer. It never rejects a
    response merely for reaching a different clinical conclusion than the pipeline's numbers
    — that would recreate exactly the "must copy numbers exactly" vs. "make clinical
    decisions" conflict this design removes.
    """
    problems: list[str] = []
    if len(text) < 200:
        problems.append("response_too_short")

    lowered = text.lower()
    if any(p in lowered[:200] for p in ("i cannot", "i can't", "i'm unable", "as an ai")):
        problems.append("response_looks_like_a_refusal")

    if text.startswith("{") and "##" not in text:
        problems.append("response_still_raw_json")

    if "radiologist verification" not in lowered and "requires radiologist" not in lowered:
        problems.append("missing_disclaimer")

    return problems


def _soft_traceability_check(text: str, findings: CoronaryCtaFindings) -> list[str]:
    """Log-only signals (never a rejection reason) that the model may have skipped the
    "always cite the pipeline's raw number" instruction — useful for monitoring/prompt
    tuning, not for gatekeeping a clinically autonomous response."""
    warnings: list[str] = []
    qs = findings.quantitative_scores
    if qs.calcium_score_valid and qs.agatston_calcium_score > 0:
        if str(round(qs.agatston_calcium_score)) not in text:
            warnings.append("agatston_score_not_visibly_cited")
    for vf in findings.vessel_findings:
        if vf.stenosis_percentage > 0 and str(round(vf.stenosis_percentage)) not in text:
            warnings.append(f"stenosis_pct_not_visibly_cited:{vf.vessel_label}")
    return warnings
