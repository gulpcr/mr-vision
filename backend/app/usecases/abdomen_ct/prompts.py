"""MedGemma payload for the abdomen_ct free-text radiological report.

The abdomen_ct use case has NO measurements — the model reads a set of axial CT
slices sampled evenly across the abdomen (rendered in a soft-tissue HU window) and
writes a report from the images alone. The prompt asks the model to first orient
itself (name the anatomical level / organs visible on each slice), then report
pathology, and forces a small JSON contract so the pipeline can store a structured
``ai_report`` (findings / impression / disclaimer).

Self-contained by design: no imports from other use-case plugins.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DISCLAIMER = (
    "AI-generated report from a limited set of sampled slices — no segmentation or "
    "measurement was performed and slices between the samples were not reviewed. "
    "Requires radiologist verification of the full study before clinical use."
)


def build_prompt(
    image_names: list[str],
    windows: list[str],
    study_description: str | None,
    selection: str = "even",
) -> str:
    """Assemble the MedGemma prompt for an abdomen-CT image-only read.

    ``windows`` is the ordered list of HU-window names each level is rendered in
    (e.g. ["soft-tissue", "liver", "bone"]). The image list is level-major: all
    windows of level 1, then level 2, … The filename suffix names each image's window.
    ``selection`` is how the levels were chosen: ``"anomaly"`` (flagged as most
    abnormal by an unsupervised model — pay them extra attention) or ``"even"``.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    n = len(image_names)
    nwin = max(1, len(windows))
    if n:
        legend = "\n".join(f"  {i}. {nm}" for i, nm in enumerate(image_names, 1))
        multi = ""
        if nwin > 1:
            multi = (
                f"\nEach anatomical LEVEL is shown in {nwin} HU windows "
                f"({', '.join(windows)}) — consecutive images with the same 'z####' are the "
                "SAME slice in different windows, NOT different anatomy. The window is named in "
                "each filename. Use the RIGHT window for each check:\n"
                "  - soft-tissue: organs, masses, fluid, bowel, lymph nodes, vessels\n"
                "  - liver: subtle hypo/hyperdense hepatic (and splenic) lesions\n"
                "  - bone: skeleton only — lytic/blastic metastases, fractures, cortical destruction\n"
            )
        if selection == "anomaly":
            provenance = (
                "these levels were FLAGGED AS THE MOST ANOMALOUS by an unsupervised model "
                "(trained on normal abdomen CT) — scrutinize them closely, but confirm from "
                "the images themselves; the flag can misfire on normal variants/artifact"
            )
        else:
            provenance = "sampled evenly across the abdomen/pelvis"
        image_intro = (
            f"You are given {n} axial CT image(s); {provenance}, "
            f"ordered SUPERIOR→INFERIOR (the levels are NOT contiguous):\n{legend}\n{multi}"
        )
    else:
        image_intro = "You are given NO images for this study.\n"

    return f"""You are a board-certified radiologist reading an ABDOMEN/PELVIS CT.{context}

{image_intro}
Method:
- For EACH level, first state the anatomical level and the organs visible (e.g. \
"liver, spleen, stomach" or "kidneys, pancreas" or "pelvis, bladder"). Identify the \
organs yourself from the image.
- Then systematically assess, using the appropriate window: liver, gallbladder/biliary, \
spleen, pancreas, adrenals, kidneys/collecting systems, stomach and bowel, \
mesentery/peritoneum (free fluid, free air), lymph nodes, abdominal aorta/vessels, and \
the visualized bones (check the BONE-window images for skeletal lesions).
- Report any pathological finding you can actually SEE — mass/lesion, organ \
enlargement, hydronephrosis, calculus, bowel dilatation/wall thickening, \
lymphadenopathy, free fluid or free air, aneurysm, bone lesion — with the level (z) it \
appears on.
- Do NOT invent measurements, HU values, or findings that are not visible. Do not \
report an organ as abnormal unless the image shows it. Do not double-count the same \
finding seen across multiple windows of the same level.
- Because only a few non-contiguous levels are provided, EXPLICITLY state that the \
assessment is limited and that unshown slices were not reviewed.
- If the images are non-diagnostic or ambiguous, say so plainly rather than guessing.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<per-region findings across the sampled levels — organ identification and any abnormality, referencing the level (z); 3-8 sentences>",
  "impression": "<concise overall impression and any recommended correlation/next step; 1-3 sentences>",
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def build_payload(
    *,
    images: list[tuple[str, bytes]],
    windows: list[str],
    study_description: str | None,
    max_images: int = 0,
    selection: str = "even",
) -> dict[str, Any]:
    """Assemble the MedGemma payload from ordered ``(name, png_bytes)`` images.

    ``windows`` is the ordered list of HU-window names each level was rendered in.
    ``max_images`` of 0 (the default) sends every image — the pipeline's slice_count ×
    windows already governs the count, so the caller need not cap again. ``selection``
    is how the levels were chosen ("anomaly" | "even"). The caller supplies the bytes
    (read from disk), so this module does no I/O. Returns ``{prompt, images, image_names}``.
    """
    selected = images[: max_images] if max_images and max_images > 0 else images
    if len(images) > len(selected):
        logger.warning("abdomen_ct_images_capped", cap=max_images, available=len(images))
    names = [n for n, _ in selected]
    prompt = build_prompt(names, windows, study_description, selection=selection)
    return {"prompt": prompt, "images": [b for _, b in selected], "image_names": names}


def parse_report(raw: str) -> dict[str, str] | None:
    """Parse MedGemma's JSON response into ``{findings, impression, disclaimer}``.

    Tolerates markdown fences / surrounding prose. Returns None when nothing
    usable can be extracted so the caller can keep the deterministic summary.
    """
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
        logger.warning("abdomen_ct_report_parse_failed", error=str(exc))
        return None
    if not isinstance(data, dict):
        return None

    findings = str(data.get("findings", "") or "").strip()
    impression = str(data.get("impression", "") or "").strip()
    if not findings and not impression:
        return None
    return {
        "findings": findings,
        "impression": impression,
        "disclaimer": str(data.get("disclaimer", "") or "").strip() or _DISCLAIMER,
    }
