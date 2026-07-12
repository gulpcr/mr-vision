"""MedGemma prompts for the abdomen_ct2 two-pass read.

Two distinct prompts, one per pass:

  * :func:`build_scan_prompt` — Pass 1. Given a BATCH of axial slices (each labelled
    with its z index), MedGemma flags which slices show a potential abnormality. The
    whole volume is covered by calling this once per batch until every slice is seen.
    :func:`parse_scan_response` turns the JSON reply into ``[{z, finding}]``.

  * :func:`build_report_prompt` — Pass 2. Given only the FLAGGED slices (optionally in
    several HU windows), MedGemma writes the structured free-text report.
    :func:`parse_report` turns the JSON reply into ``{findings, impression, disclaimer}``.

Self-contained by design: no imports from other use-case plugins. The MedGemma client
is constructed and called by the Celery task hook / ``report.py``, not here.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DISCLAIMER = (
    "AI-generated report. MedGemma scanned the volume slice-by-slice to flag "
    "potentially abnormal levels, then reported on the flagged slices only — "
    "intervening and non-flagged slices were not individually reported, and no "
    "measurement or segmentation was performed. Requires radiologist verification of "
    "the full study before clinical use."
)

# The abnormalities MedGemma is asked to look for in both passes (kept identical so
# the scan pass and the report pass are consistent about what "abnormal" means).
_TARGETS = (
    "mass/lesion, organ enlargement, hydronephrosis, renal/biliary calculus, bowel "
    "dilatation or wall thickening, lymphadenopathy, free fluid or free air, aortic "
    "aneurysm, focal abnormal density, or a bone lesion"
)

# What each HU window is best at, so the model applies the right scrutiny per window.
_WINDOW_HINT = {
    "soft-tissue": "organs, masses, fluid, bowel, lymph nodes, vessels",
    "liver": "subtle hypo/hyperdense hepatic and splenic lesions",
    "bone": "the skeleton only — lytic/blastic metastases, fractures, cortical destruction",
}


# ── Pass 1: scan ─────────────────────────────────────────────────────────────

def build_scan_prompt(
    batch: list[dict[str, Any]],
    study_description: str | None = None,
    window: str = "soft-tissue",
) -> str:
    """Prompt for one scan batch. ``batch`` is an ordered list of ``{"z": int, ...}``.

    Each image the caller sends corresponds, in order, to one entry in ``batch``; the
    prompt tells MedGemma the z index of each image so it can flag slices by z.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    legend = "\n".join(f"  Image {i}: slice z={e['z']}" for i, e in enumerate(batch, 1))
    zs = ", ".join(str(e["z"]) for e in batch)

    return f"""You are a board-certified radiologist triaging an ABDOMEN/PELVIS CT.{context} \
No clinical history is assumed — assess each image on its own merits.

You are shown {len(batch)} axial CT image(s) rendered in the {window} HU window, ordered \
superior→inferior. The images are given in this exact order; each corresponds to one slice:
{legend}

This is a sensitivity-first triage (a radiologist reviews everything you flag), but flag \
a slice ONLY when you can point to a SPECIFIC abnormal finding on it — {_TARGETS}, or a \
focal asymmetry, abnormal contour, abnormal density, mass, or fluid collection. \
CRITICAL: NORMAL organs, NORMAL bowel, and NORMAL vessels are NOT findings. Do NOT flag a \
slice merely because organs / bowel loops / vessels are visible on it, or because it looks \
anatomically busy or complex — that is normal anatomy, not pathology. Use this {window} \
window for what it shows best ({_WINDOW_HINT.get(window, 'the visible structures')}).

Rules:
- Judge only from what is visible; do NOT invent measurements.
- In "finding", NAME the specific abnormality (e.g. "right adnexal mass", "hydronephrosis", \
"free fluid"). If the most you can say is that the organs/bowel/vessels look normal, that is \
NOT a finding — do not flag the slice.
- Refer to each slice by the exact z index of its image, listed above ({zs}).
- Return an empty "anomalies" list for any image that shows only normal anatomy.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "anomalies": [{{"z": <int>, "finding": "<short description of what looks abnormal>"}}],
  "reviewed": [<every z index you reviewed>]
}}
"""


def parse_scan_response(raw: str, valid_z: set[int]) -> list[dict[str, Any]]:
    """Parse a scan reply into ``[{z:int, finding:str}]``, keeping only ``valid_z``.

    Tolerates markdown fences / surrounding prose. Returns ``[]`` when nothing usable
    is present (so the caller simply treats the batch as clean).
    """
    data = _loads(raw)
    if not isinstance(data, dict):
        return []
    hits: dict[int, str] = {}
    for item in data.get("anomalies") or []:
        if not isinstance(item, dict):
            continue
        try:
            z = int(item.get("z"))
        except (TypeError, ValueError):
            continue
        if z not in valid_z:
            continue
        finding = str(item.get("finding", "") or "").strip()
        # First mention wins; keep the most descriptive if a later one is longer.
        if z not in hits or len(finding) > len(hits[z]):
            hits[z] = finding
    return [{"z": z, "finding": hits[z]} for z in sorted(hits, reverse=True)]


# ── Pass 2: report ───────────────────────────────────────────────────────────

def build_report_prompt(
    report_layout: list[dict[str, Any]],
    windows: list[str],
    study_description: str | None,
    flagged: list[dict[str, Any]] | None = None,
    any_flagged: bool = True,
) -> str:
    """Prompt for the reporting pass over the flagged (or fallback) slices.

    ``report_layout`` is the ordered, image-by-image list of ``{"z", "window"}`` for
    the images actually being sent — so the prompt can describe each image by its
    POSITION in the sequence (which the model can perceive), never by a filename it
    cannot see. ``flagged`` are the Pass-1 hints (``[{z, finding}]``). When
    ``any_flagged`` is False the scan found nothing and these are representative slices.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    nwin = max(1, len(windows))
    report_z = sorted({int(e["z"]) for e in report_layout}, reverse=True)

    # Positional legend: image N → (z, window). This is the only reliable way to tell
    # the model which image is which — it receives raw bytes in order, not filenames.
    legend = "\n".join(
        f"  Image {i}: slice z={e['z']}"
        + (f", {e['window']} window" if e.get("window") else "")
        for i, e in enumerate(report_layout, 1)
    )

    if any_flagged:
        hints = ""
        if flagged:
            hint_lines = "\n".join(
                f"  z={f['z']}: {f['finding']}" for f in flagged if f.get("finding")
            )
            if hint_lines:
                hints = (
                    "\nA first-pass scan flagged these levels as potentially abnormal "
                    "(hints — the [window] tag says where it was seen; confirm from the images, "
                    "they may misfire):\n" + hint_lines + "\n"
                )
        provenance = (
            f"The {len(report_z)} axial level(s) below (z={report_z}) were FLAGGED as potentially "
            "abnormal while scanning the whole volume; report on them.\n" + hints
        )
    else:
        provenance = (
            f"A scan of the whole volume flagged NO focal abnormality. The {len(report_z)} "
            f"level(s) below (z={report_z}) are representative slices sampled across the study; "
            "report accordingly (state plainly if there is no acute focal abnormality on them).\n"
        )

    layout_note = (
        f"\nYou are shown {len(report_layout)} image(s), in this exact order:\n{legend}\n"
    )
    if nwin > 1:
        layout_note += (
            f"So each level appears in {nwin} HU windows back-to-back (same z, different window) "
            "— those are the SAME slice, NOT different anatomy. Use each window for what it shows "
            "best (soft-tissue: organs/masses/fluid/nodes; liver: subtle hepatic/splenic lesions; "
            "bone: skeletal lesions/fractures) and do NOT double-count one finding across windows "
            "of the same level.\n"
        )

    return f"""You are a board-certified radiologist reading an ABDOMEN/PELVIS CT.{context} \
No clinical history is assumed — assess systematically from the images alone.

{provenance}{layout_note}
Method:
- For each level, first state the anatomical level and the organs visible (identify \
them yourself from the image), then report any pathological finding you can actually \
SEE — {_TARGETS} — referencing the level (z) it appears on.
- Do NOT invent measurements, HU values, or findings that are not visible, and do not \
report an organ as abnormal unless the image shows it.
- Because only selected levels are shown, EXPLICITLY state the assessment is limited \
and that unshown slices were not reviewed.
- If the images are non-diagnostic or ambiguous, say so plainly rather than guessing.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<per-region findings across the shown levels — organ identification and any abnormality, referencing the level (z); 3-8 sentences>",
  "impression": "<concise overall impression and any recommended correlation/next step; 1-3 sentences>",
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def parse_report(raw: str) -> dict[str, str] | None:
    """Parse a report reply into ``{findings, impression, disclaimer}``.

    Tolerates markdown fences / surrounding prose. Returns None when nothing usable
    can be extracted so the caller keeps the deterministic summary.
    """
    data = _loads(raw)
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


# ── shared JSON extraction ─────────────────────────────────────────────────────

def _loads(raw: str) -> Any:
    """Best-effort JSON parse tolerating markdown fences / surrounding prose."""
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
        return json.loads(text)
    except Exception as exc:
        logger.warning("abdomen_ct2_json_parse_failed", error=str(exc))
        return None
