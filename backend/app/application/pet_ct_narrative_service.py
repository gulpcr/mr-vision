from __future__ import annotations

"""Generate the pet_ct PDF report's SCAN FINDINGS + CONCLUSIONS by having Gemini read the
actual MIP/fused PET-CT images together with the pipeline's computed lesion list and
whole-body summary — acting as a radiologist correlating images with quantitative evidence,
rather than the deterministic per-region template in ``reports/pdf_generator.py``.

Grounding: the prompt supplies the full detected-lesion list and summary metrics as the
factual floor. Gemini may describe visual patterns beyond that list (symmetry, diffuse vs.
focal uptake, image quality) but is instructed never to invent SUV/HU/volume numbers not
given. On any failure, unavailability, or malformed response, returns None so the caller
(``pdf_generator.py``) falls back to the existing deterministic text — this must never break
report generation.
"""

import json
import re
from typing import Any

import structlog

from app.infrastructure.llm.gemini_client import GeminiClient

logger = structlog.get_logger(__name__)

_SCAN_FINDINGS_KEYS = ("head_neck", "thorax", "abdomen_pelvis", "bones_marrow")

_DEFAULT_DISCLAIMER = (
    "AI-generated radiology findings — requires radiologist verification before clinical use."
)

_PROMPT_TEMPLATE = """\
You are a board-certified nuclear medicine radiologist reading a whole-body {tracer} PET-CT scan.

You are given six images of the same study:
  1. MIP (maximum intensity projection) — axial view
  2. MIP — coronal view
  3. MIP — sagittal view
  4. Fused PET/CT — axial view (detected foci outlined in cyan)
  5. Fused PET/CT — coronal view (detected foci outlined in cyan)
  6. Fused PET/CT — sagittal view (detected foci outlined in cyan)
{clinical_block}
You are ALSO given the pipeline's computed evidence below. Its QUANTITATIVE values (SUVmax, \
SUVmean, SUVpeak, TLR, volume, CT density/HU, dimensions) are ground truth measurements and must \
not be contradicted or replaced with different numbers.

Its per-focus "structure" field is different: it is a best-effort automated label from an \
anatomical segmentation atlas with known gaps — the atlas has no "rectum" class (a rectal lesion \
is labeled "colon"), no "uterus" class (a uterine lesion is unnamed pelvic soft tissue), no \
individual lymph-node classes, and no "buccal mucosa"/oral-cavity class. Do not treat "structure" \
as unquestionable ground truth. Use the actual images — the focus's location, shape, and \
relationship to adjacent organs — to describe the anatomic site as precisely as a radiologist \
reading the scan would, even when that means naming a structure the atlas could not (e.g. a \
"colon"-labeled focus that sits in the pelvis adjacent to the bladder and vagina/prostate, with a \
tubular/narrowed shape continuous with the rectosigmoid, should be described as rectal/ \
rectosigmoid, not generic colon; an unnamed pelvic mass with a midline uterine contour should be \
described as likely uterine). Keep the quantitative values exactly as given regardless of how you \
redescribe the anatomic site.

{lesions_block}
{reference_organs_block}
{summary_block}

Instructions:
- Correlate the images with the evidence above and write findings as a real radiologist would — \
do not just restate the numbers as a list.
- You MAY describe visual patterns not captured in the structured data (symmetry, diffuse vs. \
focal uptake pattern, overall image quality, laterality) — this is expected of you.
- You must NOT invent specific SUVmax, CT density (HU), or volume values beyond what is given \
above. Any quantitative claim must trace back to the evidence provided.
- Explain physiologic vs. pathologic uptake reasoning where relevant (e.g. renal/bladder \
excretion, brown fat, bowel activity) rather than reporting every focus as disease.
- Do NOT describe or report the brain. Physiologic cerebral FDG uptake is normal, expected in \
every study, and is excluded from detection upstream — it must never appear in the findings. Do \
not mention brain parenchyma, cerebral/brain uptake, or any brain lesion in the head_neck \
paragraph or anywhere else, even to call it normal.
- For every region, identify the single largest and/or most FDG-avid unnamed or ambiguously \
labeled mass and explicitly assess whether it could be a primary tumor arising from a specific \
organ — based on its own shape and location alone, independent of whether clinical history is \
provided. A bulky, rounded/ovoid mass is more consistent with an organ-of-origin primary (e.g. \
uterine, ovarian, renal, hepatic) than with bowel; an elongated, tubular mass following a \
bowel-loop contour is more consistent with a colonic/rectal process. State your best assessment \
(e.g. "most consistent with a uterine primary given its bulky, midline pelvic location") rather \
than defaulting to a generic descriptor such as "pelvic mass" or "soft-tissue lesion" — you may \
hedge ("likely", "favor") but must not omit the assessment entirely just because the automated \
structure label is absent.
- For each region, reference the specific anatomic subdivisions relevant to what is imaged — as a \
radiologist would when correlating against labeled cross-sectional anatomy — rather than \
describing the region only in coarse terms. Only name a subdivision actually relevant to the \
images/evidence; do not pad with anatomy that has nothing to report:
    head_neck: oral cavity, tongue, nasopharynx, oropharynx, hypopharynx/larynx, thyroid bed, \
parotid and submandibular glands, cervical lymph node levels (I-VI), skull base, calvarium \
(the brain parenchyma is excluded — do not mention it; see above).
    thorax: lung lobes (upper/middle/lower, right/left), mediastinal compartments (anterior/ \
middle/posterior), hila, pleura, chest wall, axillae, breast tissue if imaged.
    abdomen_pelvis: liver, spleen, pancreas, adrenal glands, kidneys, stomach, small bowel, colon, \
rectum, peritoneum/mesentery, bladder, and pelvic organs (uterus/ovaries or prostate/seminal \
vesicles as applicable).
    bones_marrow: named vertebral levels (e.g. L2, T8), skull, ribs, sternum, pelvis, and long \
bones — with a note on marrow uptake pattern (focal vs. diffuse).
- Write ONE finding paragraph (1-5 sentences) for EACH of these four anatomical regions, even \
when there is nothing abnormal (state a normal/negative finding in that case, still naming the \
subdivisions assessed):
    head_neck, thorax, abdomen_pelvis, bones_marrow
- Write CONCLUSIONS as a short list of bullet-point strings: overall impression, a clear \
tumor-positive/negative call, and anything relevant to staging or follow-up.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "scan_findings": {{
    "head_neck": "<finding paragraph>",
    "thorax": "<finding paragraph>",
    "abdomen_pelvis": "<finding paragraph>",
    "bones_marrow": "<finding paragraph>"
  }},
  "conclusions": ["<bullet 1>", "<bullet 2>"],
  "disclaimer": "{disclaimer}"
}}
"""


class PetCtNarrativeService:
    """Generates the pet_ct report's findings/conclusions via Gemini Vision."""

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
        clinical_indication: str | None = None,
        clinical_history: str | None = None,
    ) -> dict[str, Any] | None:
        """Returns {"scan_findings": {...}, "conclusions": [...], "disclaimer": str}, or None
        on failure/unavailability/malformed response — callers must fall back to the
        deterministic template in that case.

        ``clinical_indication``/``clinical_history`` are the referring reason-for-study and
        history (from patient onboarding), when recorded — the same context a radiologist
        would have on hand. Optional: most of this pipeline's studies have neither filled in,
        so findings must remain sound without it (see the primary-tumor inference instruction,
        which reasons from imaging shape/location alone)."""
        if not self.available or not images:
            return None

        prompt = _build_prompt(summary, measurements, clinical_indication, clinical_history)
        try:
            raw = await self._client.generate_from_images(prompt, list(images.values()))
            parsed = _parse_json_response(raw)
            validated = _validate_and_normalise(parsed)
            if validated is None:
                logger.warning("petct_narrative_invalid_response", raw_preview=raw[:300])
            return validated
        except Exception as exc:
            logger.warning("petct_narrative_generation_failed", error=str(exc))
            return None


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_prompt(
    summary: dict[str, Any],
    measurements: dict[str, Any],
    clinical_indication: str | None = None,
    clinical_history: str | None = None,
) -> str:
    clinical_lines = []
    if clinical_indication:
        clinical_lines.append(f"  - Reason for study: {clinical_indication}")
    if clinical_history:
        clinical_lines.append(f"  - Clinical history: {clinical_history}")
    clinical_block = (
        "\nReferring clinical context:\n" + "\n".join(clinical_lines) + "\n"
        if clinical_lines
        else ""
    )

    lesions = measurements.get("lesions", []) or []
    lesion_lines = []
    for le in lesions:
        parts = [f"id={le.get('id')}", f"region={le.get('anatomical_region')}"]
        if le.get("structure"):
            parts.append(f"structure={le['structure']}")
        if le.get("physiologic_uptake"):
            parts.append("physiologic_uptake=true")
        for key in ("suv_max", "suv_mean", "suv_peak", "tlr", "volume_ml", "ct_mean_hu"):
            val = le.get(key)
            if isinstance(val, (int, float)):
                parts.append(f"{key}={val:.2f}")
        dims = le.get("dimensions_cm")
        if dims:
            parts.append("dimensions_cm=" + "x".join(f"{d:.1f}" for d in dims))
        lesion_lines.append("  - " + ", ".join(parts))
    lesions_block = (
        "Detected foci (" + str(len(lesions)) + " total):\n" + "\n".join(lesion_lines)
        if lesion_lines
        else "Detected foci: none — no FDG-avid focus met the detection threshold."
    )

    ref_organs = (measurements.get("reference_organs") or {})
    ref_lines = [f"  - {k.replace('_', ' ')}: {v}" for k, v in ref_organs.items()]
    reference_organs_block = (
        "Reference organ SUV (used as the physiologic-background baseline):\n" + "\n".join(ref_lines)
        if ref_lines
        else ""
    )

    sum_lines = []
    for key in (
        "diagnosis", "deauville_score", "percist_score", "tumor_to_liver_ratio",
        "mtv_total_ml", "tlg_total", "suvmax_body", "inference_method",
        "confidence", "confidence_reasons", "quantitative",
    ):
        if key in summary and summary[key] not in (None, "", []):
            sum_lines.append(f"  - {key.replace('_', ' ')}: {summary[key]}")
    summary_block = ("Whole-body summary:\n" + "\n".join(sum_lines)) if sum_lines else ""

    tracer = summary.get("radiopharmaceutical") or "18F-FDG"

    return _PROMPT_TEMPLATE.format(
        tracer=tracer,
        clinical_block=clinical_block,
        lesions_block=lesions_block,
        reference_organs_block=reference_organs_block,
        summary_block=summary_block,
        disclaimer=_DEFAULT_DISCLAIMER,
    )


def _parse_json_response(text: str) -> dict[str, Any]:
    """Parse JSON from Gemini response, tolerating surrounding prose and markdown fences."""
    text = text.strip()
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


def _validate_and_normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Returns the normalised report dict, or None if the response is structurally unusable
    (missing/empty required fields) so the caller falls back to the deterministic template."""
    scan_findings_raw = raw.get("scan_findings")
    if not isinstance(scan_findings_raw, dict):
        return None

    scan_findings: dict[str, str] = {}
    for key in _SCAN_FINDINGS_KEYS:
        text = scan_findings_raw.get(key)
        if not isinstance(text, str) or not text.strip():
            return None
        scan_findings[key] = text.strip()

    conclusions_raw = raw.get("conclusions")
    if not isinstance(conclusions_raw, list) or not conclusions_raw:
        return None
    conclusions = [str(c).strip() for c in conclusions_raw if str(c).strip()]
    if not conclusions:
        return None

    disclaimer = raw.get("disclaimer")
    if not isinstance(disclaimer, str) or not disclaimer.strip():
        disclaimer = _DEFAULT_DISCLAIMER

    return {
        "scan_findings": scan_findings,
        "conclusions": conclusions,
        "disclaimer": disclaimer,
    }
