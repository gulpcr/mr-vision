"""Stage 4 — MedGemma zero-shot payload for the pet_ct AI report.

Turns the pipeline's *deterministic* measurements (the numbers are computed by the
math in Stages 1-2, never by the LLM) into a compact, grounded prompt plus the
Stage-3 images (numbered coronal roadmap + regional composite crops) for a local
MedGemma model.

Design goals:

* **Grounding, not invention.** Every quantitative value (SUVmax, dimensions,
  TotalSegmentator location, TLR) is handed to the model as a fixed DATA MATRIX.
  The model narrates those facts; it does not re-measure them.

* **Findings-First chain-of-thought.** The template makes the model state each
  lesion's anatomical location first, then its metrics, before the impression —
  locking organ names early to prevent mid-report "organ switching".

* **Same output contract as the Gemini path.** The prompt forces the SAME JSON
  schema the pipeline stores and the UI/PDF render — ``scan_findings`` (the four
  anatomical regions), ``conclusions``, ``disclaimer`` — so swapping Gemini for
  MedGemma changes *who authors* the report, not how it is stored or displayed.
  Validation/parsing is done by the caller via
  ``pet_ct_narrative_service._validate_and_normalise`` (single source of truth for
  the ai_report shape).

Token management: the lesion table is capped (top-N by SUVmax) and the image list
is capped (roadmap first, then composites); anything dropped is noted in-prompt.
"""
from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Practical bounds for a local model. Images dominate token cost (~256 tokens each
# for the vision encoder), so the image cap matters more than the text cap.
_MAX_LESION_ROWS = 25

# Must match pet_ct_narrative_service._SCAN_FINDINGS_KEYS and the UI/PDF renderers.
_SCAN_REGION_KEYS = ("head_neck", "thorax", "abdomen_pelvis", "bones_marrow")
_DISCLAIMER = (
    "AI-generated radiology findings — requires radiologist verification before clinical use."
)


def _fmt(v: Any, suffix: str = "") -> str:
    """Render a value for the data matrix, collapsing None → '—'."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}{suffix}"
    if isinstance(v, (list, tuple)):
        return " × ".join(f"{x:g}" for x in v) + suffix if v else "—"
    return f"{v}{suffix}"


def build_data_matrix(
    *,
    lesions: list[dict[str, Any]],
    summary: dict[str, Any],
    reference_organs: dict[str, Any],
    radiopharmaceutical: str,
    quantitative: bool,
    max_rows: int = _MAX_LESION_ROWS,
) -> str:
    """Build the structured lesion data-matrix text block (the model's ground truth)."""
    liver = reference_organs.get("liver_suv_mean")
    med = reference_organs.get("mediastinum_suv_mean")

    header = [
        f"TRACER: {radiopharmaceutical}",
        f"SUV CALIBRATION: {'quantitative' if quantitative else 'NON-QUANTITATIVE (relative intensities)'}",
        f"REFERENCE LIVER SUVmean: {_fmt(liver)}   MEDIASTINUM SUVmean: {_fmt(med)}",
        f"TOTAL LESIONS: {summary.get('lesion_count', len(lesions))}"
        f"   WHOLE-BODY MTV: {_fmt(summary.get('mtv_total_ml'), ' mL')}"
        f"   TOTAL TLG: {_fmt(summary.get('tlg_total'))}",
        f"HIGHEST DEAUVILLE: {_fmt(summary.get('deauville_score'))}",
    ]

    rows = [
        "LESION | LOCATION (segmented) | REGION | SUVmax | SUVpeak | TLR | LONG×SHORT cm | CT HU | VOLUME mL | PHYSIOLOGIC?",
    ]
    shown = lesions[:max_rows]
    for les in shown:
        rows.append(
            " | ".join([
                f"#{les.get('id')}",
                _fmt(les.get("structure")) if les.get("structure") else "(unnamed)",
                _fmt(les.get("anatomical_region")),
                _fmt(les.get("suv_max")),
                _fmt(les.get("suv_peak")),
                _fmt(les.get("tlr")),
                f"{_fmt(les.get('long_diameter_cm'))}×{_fmt(les.get('short_diameter_cm'))}",
                _fmt(les.get("ct_mean_hu")),
                _fmt(les.get("volume_ml")),
                "YES" if les.get("physiologic_uptake") else "no",
            ])
        )
    if len(lesions) > max_rows:
        rows.append(f"... ({len(lesions) - max_rows} additional lower-SUV foci omitted from table)")

    return "\n".join(header) + "\n\n" + "\n".join(rows)


def _describe_image(name: str) -> str:
    """One-line description of an image for the prompt's image list."""
    n = name.lower()
    if "roadmap" in n:
        return (
            "full-body coronal PET MIP 'roadmap' — every detected lesion is marked "
            "with a numbered dot (#n) at its body location"
        )
    if "composite" in n:
        return (
            "regional composite — CT (grayscale, soft-tissue window) fused with PET "
            "(hot colormap), lesion boundaries outlined in cyan, each lesion labelled #n"
        )
    if "fused" in n:
        return "fused PET/CT view (detected foci outlined in cyan)"
    if "mip" in n:
        return "PET maximum-intensity-projection view"
    return "PET/CT image"


def build_petct_prompt(data_matrix: str, image_names: list[str], radiopharmaceutical: str) -> str:
    """Assemble the MedGemma prompt: image legend + data matrix + Findings-First + JSON schema.

    Outputs the SAME ai_report JSON contract the pipeline stores and the UI renders.
    """
    if image_names:
        legend = "\n".join(
            f"  {i}. {nm} — {_describe_image(nm)}" for i, nm in enumerate(image_names, 1)
        )
        image_intro = f"You are given {len(image_names)} image(s):\n{legend}\n"
    else:
        image_intro = "You are given NO images for this study — reason from the DATA MATRIX alone.\n"

    return f"""You are a board-certified nuclear medicine radiologist reading a whole-body \
{radiopharmaceutical} PET-CT scan.

{image_intro}
You are ALSO given the pipeline's deterministic measurements as a DATA MATRIX. Its numeric \
values (SUVmax, SUVpeak, TLR, dimensions, CT HU, volume, reference SUV) are GROUND TRUTH — never \
contradict them or substitute different numbers. The per-lesion "LOCATION (segmented)" is a \
best-effort automated label with known gaps (no rectum/uterus/individual-node classes); refine \
the anatomic site from the images where the shape/location makes it clear, but keep every number \
exactly as given.

Method — FINDINGS FIRST:
- State each lesion's anatomical location FIRST, then its metrics; do not change a lesion's stated \
organ later in the report.
- Refer to lesions by their #number (matching the labels burned into the images).
- Group findings into the four regions below; for a region with no lesion, give a normal/negative \
statement naming the subdivisions assessed.
- Do NOT report the brain — physiologic cerebral uptake is excluded upstream and must never appear.
- Treat any lesion flagged PHYSIOLOGIC? = YES as likely normal excretory/organ uptake, not tumour, \
unless the images clearly contradict it.
- If SUV CALIBRATION is NON-QUANTITATIVE, describe uptake relatively and say so.
- ANATOMICAL LOCALIZATION FROM IMAGES: The "LOCATION (segmented)" column represents a best-effort automated structural classifier with known class gaps. For any lesion labeled "(unnamed)", you must visually cross-reference its position on the regional composite image against surrounding recognizable anatomy. State its precise clinical location (e.g., specific organ wall, deep muscle compartment, or nodal station) in the text findings based on visual inspection.

- DISEASE HIERARCHY DEDUCTION: Analyze the whole-body distribution of the lesions globally:
  1. Primary Mass Identification: Look for the dominant metabolic focus—typically characterized by an extreme, outlier SUVmax/volume relative to the rest of the body (the "index lesion"). Deduce and name the specific organ or tissue structure acting as this primary epicenter.
  2. Metastatic Mapping: Characterize secondary, lower-intensity hypermetabolic foci relative to the primary mass (e.g., regional vs. distant lymphadenopathy, solid organ metastases, or skeletal involvement).
  3. Non-Biopsied Differential Phrasing: If no prior clinical history is provided, use diagnostic radiological verbiage to describe your synthesis (e.g., "Highly concerning for a primary [Organ Name] malignancy with widespread secondary nodal and muscular metastases").

  - ONCOLOGICAL LOCALIZATION RULES (VISCERA & NODES OVER MUSCLES):
  1. Visceral Organ Grounding: You must describe findings in the context of visceral organs, gastrointestinal tracts, or specific solid organs (e.g., stomach, pancreas, liver, lungs) rather than surrounding musculoskeletal structures.
  2. Translate Musculoskeletal Segments: If the Data Matrix or images show a lesion localized to a skeletal muscle compartment (such as 'iliopsoas', 'psoas', or 'abdominal wall'), do NOT describe it as a primary muscle lesion. Instead, clinically translate this location to its true oncological context: evaluate if it represents retroperitoneal/pelvic lymphadenopathy (lymph nodes tracking along those pathways) or peritoneal/omental carcinomatosis (spread across the abdominal lining).
  3. Image-Driven Organ Mapping: For any "(unnamed)" lesion, cross-reference its visual position on the composite slice with visceral anatomy. If a massive index lesion (like #2) is located in the sub-diaphragmatic upper abdomen near the left upper quadrant, map it strictly to the stomach/gastric wall, avoiding lower GI structures like the cecum unless visually definitive.

- CLINICAL HIERARCHY SYNTHESIS:
  1. Primary Index Lesion: Identify the single dominant metabolic epicenter (outlier SUVmax/volume). Explicitly state the suspected primary organ of origin based on whole-body distribution patterns.
  2. Secondary Metastatic Cascade: Describe all other lesions as secondary structural involvement, categorizing them strictly as either: regional/distant lymph node stations, solid organ metastases, or peritoneal seeding.
=== DATA MATRIX ===
{data_matrix}
=== END DATA MATRIX ===

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "scan_findings": {{
    "head_neck": "<oral cavity, tongue, naso/oro/hypopharynx, larynx, thyroid bed, parotid/submandibular glands, cervical node levels, skull base/calvarium — 1-5 sentences>",
    "thorax": "<lung lobes, mediastinal compartments, hila, pleura, chest wall, axillae, breast tissue — 1-5 sentences>",
    "abdomen_pelvis": "<liver, spleen, pancreas, adrenals, kidneys, stomach, bowel, peritoneum, bladder, pelvic organs — 1-5 sentences>",
    "bones_marrow": "<named vertebral levels, skull, ribs, sternum, pelvis, long bones; focal vs diffuse marrow uptake — 1-5 sentences>"
  }},
  "conclusions": ["<overall disease burden / impression>", "<clear tumor-positive or -negative call>", "<staging or follow-up recommendation>"],
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def build_payload(
    *,
    summary: dict[str, Any],
    measurements: dict[str, Any],
    images: list[tuple[str, bytes]],
    radiopharmaceutical: str,
    quantitative: bool,
    max_images: int,
) -> dict[str, Any]:
    """Assemble the complete MedGemma payload from measurements + ordered images.

    ``images`` is an ordered list of ``(name, png_bytes)`` — the caller supplies the
    bytes (from local disk in the pipeline, or object storage in the debug endpoint),
    so this module stays free of any I/O. Returns ``{prompt, images, image_names}``.
    """
    selected = images[: max_images if max_images and max_images > 0 else len(images)]
    if len(images) > len(selected):
        logger.warning("medgemma_images_capped", cap=max_images, available=len(images))

    data_matrix = build_data_matrix(
        lesions=measurements.get("lesions", []) or [],
        summary=summary,
        reference_organs=measurements.get("reference_organs", {}) or {},
        radiopharmaceutical=radiopharmaceutical,
        quantitative=quantitative,
    )
    names = [n for n, _ in selected]
    prompt = build_petct_prompt(data_matrix, names, radiopharmaceutical)
    return {"prompt": prompt, "images": [b for _, b in selected], "image_names": names}
