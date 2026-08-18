"""MedGemma prompts for the abdomen_ct two-pass read.

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

REGION PROFILE: every body-part-specific string in this module lives in the ``REGION``
dict below (targets, organ review, window hints, anatomical phrasing). To retarget this
read to another CT region, copy this file into the new plugin and swap ``REGION`` only —
everything else is region-agnostic scaffolding. The HU windows themselves live in the
plugin's ``model/inference_config.yaml``.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Region profile (the ONLY body-part-specific content — swap this per plugin) ──
REGION: dict[str, Any] = {
    # How the read is framed ("triaging <exam_phrase>", "reading <exam_phrase>").
    "exam_phrase": "an ABDOMEN/PELVIS CT",
    # Anatomical span, used in "spanning <region_words>".
    "region_words": "the abdomen and pelvis",
    # Image descriptor for the detailed-read prompt.
    "richread_image_phrase": "axial soft-tissue images spanning the abdomen and pelvis",
    # The abnormalities to look for in BOTH passes (kept identical so scan and report
    # agree on what "abnormal" means).
    "targets": (
        "mass/lesion, organ enlargement, hydronephrosis, renal/biliary calculus, bowel "
        "dilatation or wall thickening, lymphadenopathy, free fluid or free air, aortic "
        "aneurysm, focal abnormal density, or a bone lesion"
    ),
    # What each HU window is best at, so the model applies the right scrutiny per window.
    "window_hints": {
        "soft-tissue": "organs, masses, fluid, bowel, lymph nodes, vessels",
        "liver": "subtle hypo/hyperdense hepatic and splenic lesions",
        "bone": "the skeleton only — lytic/blastic metastases, fractures, cortical destruction",
    },
    # Scan pass: what normal anatomy looks like here, so the model does not flag it.
    "scan_normal_clause": (
        "NORMAL organs, NORMAL bowel, and NORMAL vessels are NOT findings. Do NOT flag a "
        "slice merely because organs / bowel loops / vessels are visible on it, or because "
        "it looks anatomically busy or complex — that is normal anatomy, not pathology."
    ),
    # Scan pass: example named findings (teaches the model to NAME, not describe vaguely).
    "scan_finding_examples": '"right adnexal mass", "hydronephrosis", "free fluid"',
    # Report pass: per-window scrutiny hint, only used when >1 window is shown per level.
    "report_multiwindow_detail": (
        "soft-tissue: organs/masses/fluid/nodes; liver: subtle hepatic/splenic lesions; "
        "bone: skeletal lesions/fractures"
    ),
    # Detailed read: the systematic organ/structure checklist to walk through.
    "organ_review": (
        "liver, gallbladder, spleen, pancreas, adrenals, kidneys, bowel, "
        "mesentery/peritoneum (free fluid/air), abdominal aorta/vessels, lymph nodes, and "
        "the visualized bones"
    ),
    # Detailed read: adjacent-structure relationship examples for characterization.
    "relationship_examples": (
        "e.g. abutting the rectum, in contact with the urinary bladder, abutting the "
        "sigmoid colon / bowel loops, fat planes preserved or indistinct"
    ),
}

_DISCLAIMER = (
    "AI-generated report. MedGemma scanned the volume slice-by-slice to flag "
    "potentially abnormal levels, then reported on the flagged slices only — "
    "intervening and non-flagged slices were not individually reported, and no "
    "measurement or segmentation was performed. Requires radiologist verification of "
    "the full study before clinical use."
)

_TARGETS = REGION["targets"]
_WINDOW_HINT = REGION["window_hints"]


# ── Pass 1: scan ─────────────────────────────────────────────────────────────

def build_scan_prompt(
    batch: list[dict[str, Any]],
    study_description: str | None = None,
    window: str = "soft-tissue",
    demographics: str | None = None,
) -> str:
    """Prompt for one scan batch. ``batch`` is an ordered list of ``{"z": int, ...}``.

    Each image the caller sends corresponds, in order, to one entry in ``batch``; the
    prompt tells MedGemma the z index of each image so it can flag slices by z.

    ``demographics`` (e.g. "female, 55 years"), when supplied, replaces the age-blind
    clause with a normal-for-age calibration clause — used ONLY to judge what is normal
    for age, never as a directive to hunt age-associated disease.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    legend = "\n".join(f"  Image {i}: slice z={e['z']}" for i, e in enumerate(batch, 1))
    zs = ", ".join(str(e["z"]) for e in batch)

    if demographics:
        history_clause = (
            f"Patient: {demographics}. Use age ONLY to calibrate what is normal for age "
            "(organ proportions, thymus, marrow, unfused growth plates, age-expected "
            "involution) — do NOT assume or hunt for any age-associated diagnosis. Assess "
            "each image on its own merits."
        )
    else:
        history_clause = "No clinical history is assumed — assess each image on its own merits."

    return f"""You are a board-certified radiologist triaging {REGION['exam_phrase']}.{context} \
{history_clause}

You are shown {len(batch)} axial CT image(s) rendered in the {window} HU window, ordered \
superior→inferior. The images are given in this exact order; each corresponds to one slice:
{legend}

This is a sensitivity-first triage (a radiologist reviews everything you flag), but flag \
a slice ONLY when you can point to a SPECIFIC abnormal finding on it — {_TARGETS}, or a \
focal asymmetry, abnormal contour, abnormal density, mass, or fluid collection. \
CRITICAL: {REGION['scan_normal_clause']} Use this {window} \
window for what it shows best ({_WINDOW_HINT.get(window, 'the visible structures')}).

Rules:
- Judge only from what is visible; do NOT invent measurements.
- In "finding", NAME the specific abnormality (e.g. {REGION['scan_finding_examples']}). If the \
most you can say is that the visible structures look normal, that is NOT a finding — do not \
flag the slice.
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
            f"best ({REGION['report_multiwindow_detail']}) and do NOT double-count one finding "
            "across windows of the same level.\n"
        )

    return f"""You are a board-certified radiologist reading {REGION['exam_phrase']}.{context} \
No clinical history is assumed — assess systematically from the images alone.

{provenance}{layout_note}
Method:
- For each level, first state the anatomical level and the structures visible (identify \
them yourself from the image), then report any pathological finding you can actually \
SEE — {_TARGETS} — referencing the level (z) it appears on.
- Do NOT invent measurements, HU values, or findings that are not visible, and do not \
report a structure as abnormal unless the image shows it.
- Because only selected levels are shown, EXPLICITLY state the assessment is limited \
and that unshown slices were not reviewed.
- If the images are non-diagnostic or ambiguous, say so plainly rather than guessing.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<per-region findings across the shown levels — structure identification and any abnormality, referencing the level (z); 3-8 sentences>",
  "impression": "<concise overall impression and any recommended correlation/next step; 1-3 sentences>",
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def build_richread_prompt(
    image_zs: list[int],
    flagged: list[dict[str, Any]] | None,
    study_description: str | None,
    demographics: str | None = None,
) -> str:
    """Detailed-read prompt: characterize the findings + systematic organ review.

    Given a set of axial images spanning the region (flagged levels plus evenly-spread
    coverage), MedGemma writes a DETAILED findings paragraph the way a radiologist
    dictates: it characterizes each abnormality (location and relationship to ADJACENT
    structures) and then does a SYSTEMATIC review of the structures visible. Grounded to
    the images — no invented measurements. ``demographics`` (sex/age) helps it name
    sex-appropriate organs. ``image_zs`` is internal only (not cited).
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    if demographics and demographics.strip():
        context += f' Patient: {demographics.strip()} (use sex to name sex-appropriate organs).'
    hint = ""
    if flagged:
        items = "; ".join(
            re.sub(r"^\s*\[[^\]]*\]\s*", "", str(f.get("finding", ""))).strip()
            for f in flagged if f.get("finding")
        )
        if items:
            hint = (
                f"\nA first pass flagged: {items}. Confirm and CHARACTERIZE these on the images "
                "(they may misfire).\n"
            )

    return f"""You are a consultant radiologist reading {REGION['exam_phrase']}.{context} You are \
shown {len(image_zs)} {REGION['richread_image_phrase']} (superior→inferior).
{hint}
Dictate a DETAILED FINDINGS section the way a radiologist writes a report:
- For each abnormality you see, describe it fully: its ORGAN/anatomical location, and its \
RELATIONSHIP to ADJACENT structures visible on the images ({REGION['relationship_examples']}), \
plus its character (e.g. heterogeneous, cystic/solid, enhancing) IF visible.
- Then give a SYSTEMATIC ORGAN REVIEW of the structures visible across the images: \
{REGION['organ_review']} — stating the status of each (normal, or the abnormality seen). \
Comment on a structure only if it is actually visible.
- GROUNDING: describe ONLY what is visible. NEVER state a numeric size or measurement — no \
centimetres, millimetres, or dimensions of ANY kind (you cannot measure from these images). You \
MAY describe size qualitatively ("bulky", "large", "small") but NEVER with a number. Do NOT \
invent HU values or findings you cannot see. Do NOT mention slice/z numbers, "images", or that an \
AI produced this — write as a radiologist dictating.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<detailed radiological FINDINGS: characterized abnormalities with adjacent-structure relationships, then a systematic organ review; fluent prose, several sentences>",
  "impression": "<concise impression of the significant findings + a brief hedged recommendation>",
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def build_mass_verify_prompt(
    image_zs: list[int],
    flagged: list[dict[str, Any]] | None,
    study_description: str | None,
    demographics: str | None = None,
) -> str:
    """Adversarial SECOND-READ prompt: is the flagged mass REAL, or normal anatomy?

    Framed to DISPROVE the mass (a first-pass screen over-calls), so the model is biased
    the opposite way to the scan pass. Called several times and majority-voted by
    ``report.verify_mass_vlm``. Returns a JSON verdict parsed by :func:`parse_mass_verify`.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    if demographics and demographics.strip():
        context += f' Patient: {demographics.strip()}.'
    hint = ""
    if flagged:
        items = "; ".join(
            re.sub(r"^\s*\[[^\]]*\]\s*", "", str(f.get("finding", ""))).strip()
            for f in flagged if f.get("finding")
        )
        if items:
            hint = f' The first pass said: "{items}".'

    return f"""You are a senior radiologist doing a CRITICAL SECOND READ. A first-pass screen \
FLAGGED a possible MASS on the {len(image_zs)} axial soft-tissue image(s) shown.{context}{hint}

First-pass screens OVER-CALL masses. On abdominal CT, these NORMAL things are routinely mistaken \
for a "mass": collapsed or fluid-filled bowel loops, un-opacified bowel and its contents, the psoas \
/ iliacus muscles, normal vessels, mesenteric fat, and partial-volume averaging. Your job is to \
DISPROVE the mass unless it is unmistakable.

Judge ONLY from the images. Is there a TRUE abnormal mass — a discrete abnormal soft-tissue \
structure NOT explained by normal anatomy — with a specific abnormal feature you can name \
(abnormal contour, heterogeneity, abnormal enhancement, effacement of fat planes, displacement of \
organs)? DEFAULT to "no" if you cannot point to such a feature; say "uncertain" only if genuinely \
equivocal.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "mass_present": "yes" | "no" | "uncertain",
  "abnormal_feature": "<the specific abnormal feature, or empty if none>",
  "normal_explanation": "<if no/uncertain: the normal structure this most likely is>",
  "reason": "<one short sentence>"
}}
"""


def parse_mass_verify(raw: str) -> dict[str, str] | None:
    """Parse an adversarial-verify reply → ``{mass_present, abnormal_feature, reason}``."""
    data = _loads(raw)
    if not isinstance(data, dict):
        return None
    mp = str(data.get("mass_present", "") or "").strip().lower()
    if mp not in ("yes", "no", "uncertain"):
        mp = "yes" if "yes" in mp else ("no" if "no" in mp else "uncertain")
    return {
        "mass_present": mp,
        "abnormal_feature": str(data.get("abnormal_feature", "") or "").strip(),
        "normal_explanation": str(data.get("normal_explanation", "") or "").strip(),
        "reason": str(data.get("reason", "") or "").strip(),
    }


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


# A VLM cannot measure from a windowed slice, so any numeric size it emits is fabricated.
# Strip measurement clauses as a safety net (the prompt also forbids them).
_MEASURE_RE = re.compile(
    r"[,;]?\s*(?:,?\s*(?:which is\s+|and\s+)?measuring\s+)?"
    r"(?:approximately\s+|approx\.?\s+|about\s+|roughly\s+|~\s*)?"
    r"\d+(?:\.\d+)?\s*(?:[-–xX×]\s*\d+(?:\.\d+)?\s*)*(?:mm|cm)\b"
    r"(?:\s+in\s+(?:its\s+)?[a-z ]+?dimension)?"
    r"(?:\s*\((?:[A-Z]{1,3}\s*[x×]\s*)+[A-Z]{1,3}\))?",
    re.IGNORECASE,
)


def strip_measurements(text: str) -> str:
    """Remove fabricated numeric size/measurement clauses from report prose."""
    if not text:
        return text
    t = _MEASURE_RE.sub("", text)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.,;])", r"\1", t)
    t = re.sub(r"([.,;]){2,}", r"\1", t)
    return t.strip()


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
        logger.warning("ct_report_json_parse_failed", error=str(exc))
        return None
