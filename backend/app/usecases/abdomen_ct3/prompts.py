"""MedGemma prompts for the abdomen_ct3 single-pass read.

  * :func:`build_scan_prompt` — the scan. Given a BATCH of axial slices (each labelled
    with its z index, in one HU window), MedGemma flags which slices look even possibly
    abnormal and gives the reason at flag time (high-sensitivity triage). The whole
    volume is covered by calling this once per batch per scan window.
    :func:`parse_scan_response` turns the JSON reply into ``[{z, finding}]``.

  * :func:`build_synthesis_prompt` — the report. TEXT-ONLY (no images): given the
    accumulated flagged findings from the scan, MedGemma composes the structured
    findings/impression report, grouping contiguous levels and dropping duplicates.
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

This is a HIGH-SENSITIVITY triage: a radiologist reviews everything you flag, so a false \
flag is cheap but a MISS is costly. For EACH image, flag it if you see ANYTHING even \
possibly abnormal or that you are unsure about — {_TARGETS}, or any focal asymmetry, \
abnormal contour/density, or structure that does not look clearly normal. When in doubt, \
FLAG IT. Use this {window} window for what it shows best \
({_WINDOW_HINT.get(window, 'the visible structures')}).

Rules:
- Judge only from what is visible; do NOT invent measurements. You do NOT need to be \
certain — a tentative description ("possible adnexal/pelvic mass", "asymmetric soft-tissue \
density") is expected and wanted.
- Refer to each slice by the exact z index of its image, listed above ({zs}).
- Return an empty "anomalies" list ONLY if the images look UNAMBIGUOUSLY normal.

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


# ── Report synthesis (text-only, from the accumulated scan flags) ──────────────

def build_synthesis_prompt(
    flagged: list[dict[str, Any]],
    study_description: str | None,
    scanned_range: tuple[int, int] | None = None,
) -> str:
    """Text-only prompt that composes the report from the scan's flagged findings.

    ``flagged`` is the accumulated ``[{z, finding}]`` from the scan pass, where each
    ``finding`` is already prefixed with the ``[window]`` it was seen in. No images are
    sent — MedGemma already saw every slice while scanning; this call just organizes
    the flagged observations into a structured report, grouping contiguous levels that
    describe the same finding and dropping obvious duplicates/contradictions.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    rng = ""
    if scanned_range:
        rng = f" The whole volume was scanned across roughly z={scanned_range[1]} (superior) to z={scanned_range[0]} (inferior)."

    if flagged:
        lines = "\n".join(
            f"  z={f['z']}: {f['finding']}" for f in flagged if f.get("finding")
        ) or "  (levels flagged without a specific description)"
        body = (
            "A high-sensitivity slice-by-slice scan of the WHOLE volume flagged the "
            "following levels as potentially abnormal, with the reason and the [HU window] "
            "it was seen in (these are triage flags — some may be false positives or the "
            "same finding seen on several adjacent levels):\n"
            f"{lines}\n\n"
            "Write the report FROM these flagged observations:\n"
            "- Group contiguous z-levels that describe the SAME finding into one item and "
            "give its z-range (a real structure spans several slices).\n"
            "- Drop obvious duplicates and clearly spurious/contradictory flags.\n"
            "- Organize findings by organ/region, referencing the z-level(s).\n"
            "- Do NOT invent measurements or add findings that were not flagged."
        )
    else:
        body = (
            "A high-sensitivity slice-by-slice scan of the WHOLE volume flagged NO level as "
            "abnormal. Write a report stating that no focal abnormality was identified on the "
            "scanned levels (do not invent findings)."
        )

    return f"""You are a board-certified radiologist finalizing an ABDOMEN/PELVIS CT report.{context} \
No clinical history is assumed.{rng}

{body}

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<per-region findings, each referencing its z-level/range; 3-8 sentences (or a clear 'no focal abnormality' statement if nothing was flagged)>",
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
        logger.warning("abdomen_ct3_json_parse_failed", error=str(exc))
        return None
