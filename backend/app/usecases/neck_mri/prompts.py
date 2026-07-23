"""MedGemma prompts for the MULTIPARAMETRIC MRI two-pass read (scan → flag → report).

Each image sent to the model is a PANEL MONTAGE of one axial level: several co-registered
MR sequences tiled side-by-side, each tile CAPTIONED with its sequence name (T1, T1+C, T2,
FLAIR, DWI, SWI, …). The SAME position in every tile is the SAME anatomy, so the model can
read a finding's signal ACROSS sequences — which is how MRI is actually interpreted.

Two prompts:

  * :func:`build_scan_prompt` — Pass 1. A batch of level-montages; MedGemma flags the
    levels showing a potential abnormality. :func:`parse_scan_response` → ``[{z, finding}]``.
  * :func:`build_report_prompt` — Pass 2. Only the FLAGGED level-montages; MedGemma writes
    the structured multiparametric report. :func:`parse_report` → ``{findings, impression,
    disclaimer}``.

Self-contained: no imports from other plugins. The MedGemma client is constructed and
called by the Celery task hook / ``report.py``, not here.

REGION PROFILE: all body-part-specific strings live in the ``REGION`` dict below. To
retarget to another MRI region, copy this file and swap ``REGION`` only.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Region profile (the ONLY body-part-specific content — swap this per plugin) ──
REGION: dict[str, Any] = {   'exam_phrase': 'a NECK MRI',
    'region_words': 'the neck',
    'richread_image_phrase': 'axial multi-sequence MR panels spanning the neck',
    'targets': 'a soft-tissue mass, cervical lymphadenopathy, a thyroid or salivary-gland lesion, '
               'a mucosal / nasopharyngeal / laryngeal mass, an abscess or fluid collection, or '
               'airway narrowing or asymmetry',
    'scan_normal_clause': 'NORMAL muscles, vessels, glands and the patent airway are NOT findings. '
                          'Do NOT flag a level merely because neck structures are visible — that '
                          'is normal anatomy.',
    'scan_finding_examples': '"right level II nodal mass", "left parotid lesion", "nasopharyngeal '
                             'soft-tissue mass"',
    'organ_review': 'the deep neck spaces, the pharynx and larynx and the airway, the thyroid '
                    'gland, the salivary glands (parotid and submandibular), the cervical lymph '
                    'node chains, the visualized cervical spine and cord, and the great vessels',
    'relationship_examples': 'e.g. abutting or encasing the carotid sheath, displacing the airway, '
                             'invading an adjacent muscle or gland, effacing the fat planes',
    'signal_patterns': 'In the neck: fluid, oedema and many soft-tissue tumours are bright on '
                       'T2/STIR; fat is bright on T1 and suppressed on STIR; a mass typically '
                       'enhances on T1+C; normal vessels show dark flow voids.',
    'specificity_rules': 'SPECIFICITY (most studies are NORMAL): require corroboration across '
                         'sequences before calling a mass, node, or lesion abnormal. Normal '
                         'glands, muscles, vessels and flow voids are NOT findings, and a small '
                         'physiologic-sized node is not lymphadenopathy.',
    'age_sensitive': False,
    'plane_word': 'axial'}

_DISCLAIMER = (
    "AI-generated report. MedGemma reviewed co-registered multi-sequence MRI panels "
    "level-by-level to flag potentially abnormal levels, then reported on the flagged "
    "levels only — intervening levels were not individually reported, and no measurement "
    "or segmentation was performed. Requires radiologist verification of the full study "
    "before clinical use."
)

_TARGETS = REGION["targets"]


def _plane_word(window: str | None) -> str:
    """The reading-plane adjective for prompt wording: the montage ``window`` when it is a
    plane name (spine reads 'sagittal'/'axial'), else the region default ('axial')."""
    if window and str(window).lower() in ("axial", "sagittal", "coronal"):
        return str(window).lower()
    return str(REGION.get("plane_word", "axial"))


def _panels(sequences: list[str] | None, plane: str = "axial") -> str:
    """Describe the montage panels AND strictly bound the model to the sequences present.

    This is the anti-hallucination guard: when only one (or few) sequences are present, the
    model must NOT invent signal on absent sequences (a real failure mode — it will narrate
    T2/FLAIR/DWI signal for a study that only has a post-contrast T1). We name the exact
    present sequences and forbid mentioning any other. ``plane`` names the montage plane.
    """
    if sequences:
        seqs = ", ".join(sequences)
        n = len(sequences)
        if n == 1:
            body = (
                f"Each image is ONE {plane} slice shown as a SINGLE labeled panel — the only "
                f"sequence available for this study is {seqs} (captioned at its top-left). "
                "This is a single-sequence study."
            )
        else:
            body = (
                f"Each image is a PANEL MONTAGE of ONE {plane} slice containing ONLY these {n} "
                f"co-registered, labeled sequence tiles: {seqs} (each captioned at its top-left). "
                "The SAME position in every tile is the SAME anatomy, so read each finding's "
                f"SIGNAL ACROSS these sequences. {REGION['signal_patterns']}"
            )
        guard = (
            f" CRITICAL GROUNDING: the ONLY sequence(s) present are {seqs}. Describe signal ONLY "
            "on these. Do NOT mention, assume, describe, or infer ANY sequence that is not in this "
            "list — if a sequence (e.g. T2, FLAIR, DWI, SWI, T1+C) is NOT present, say NOTHING "
            "about how the finding looks on it. Reporting signal on an absent sequence is a serious "
            "error."
        )
        return body + guard
    # Unknown layout — rely on the tile captions and forbid inventing sequences.
    return (
        f"Each image is a PANEL MONTAGE of ONE {plane} slice: co-registered MR sequences shown "
        "side-by-side, each tile CAPTIONED at its top-left with its sequence name. The SAME "
        "position in every tile is the SAME anatomy, so read each finding's SIGNAL ACROSS the "
        "sequences. Describe signal ONLY on sequences you can actually see as a labeled tile — "
        "do NOT mention or infer any sequence that is not shown."
    )


# ── Pass 1: scan ─────────────────────────────────────────────────────────────

def build_scan_prompt(
    batch: list[dict[str, Any]],
    study_description: str | None = None,
    window: str = "multi-sequence",
    sequences: list[str] | None = None,
    demographics: str | None = None,
) -> str:
    """Prompt for one scan batch. ``batch`` is an ordered list of ``{"z": int, ...}``;
    each entry is ONE level-montage image, in the given order. ``sequences`` is the exact
    list of sequences present in the montages (grounds the model against inventing others).
    ``demographics`` (sex, age) is used to weight flagging ONLY for age-sensitive regions
    (``REGION['age_sensitive']``); other regions ignore it, so their prompt is unchanged."""
    # NOTE: patient age is deliberately NOT used in the SCAN pass. Scanning is pure detection
    # (sensitivity) — age must not pre-judge whether to flag a level (that would suppress or
    # bias detection). Age is applied only in the interpretive rich-read/findings pass.
    context = f' The study is described as "{study_description}".' if study_description else ""
    plane = _plane_word(window)
    legend = "\n".join(f"  Image {i}: {plane} slice z={e['z']} (panel montage)" for i, e in enumerate(batch, 1))
    zs = ", ".join(str(e["z"]) for e in batch)

    return f"""You are a board-certified radiologist triaging {REGION['exam_phrase']}.{context} \
No clinical history is assumed — assess each slice on its own merits.

{_panels(sequences, plane)}

You are shown {len(batch)} {plane} panel-montage(s); each corresponds to one {plane} slice:
{legend}

MOST STUDIES ARE NORMAL — do not invent pathology. Flag a level ONLY when a SPECIFIC finding is \
CORROBORATED across the appropriate sequences — {_TARGETS}. A signal seen on only ONE sequence with \
no matching correlate on the others (e.g. DWI-bright WITHOUT a matching dark-ADC change) is usually \
artefact, T2 shine-through, or normal variation — do NOT flag it. When genuinely in doubt, do NOT \
flag. CRITICAL: {REGION['scan_normal_clause']} {REGION['specificity_rules']}

Rules:
- Judge only from what is visible across the tiles; do NOT invent measurements.
- In "finding", NAME the specific abnormality and note its cross-sequence signal if helpful \
(e.g. {REGION['scan_finding_examples']}). If the most you can say is that the structures look \
normal, that is NOT a finding — do not flag the level.
- Refer to each level by the exact z index listed above ({zs}).
- Return an empty "anomalies" list for any level that shows only normal anatomy.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "anomalies": [{{"z": <int>, "finding": "<short description of what looks abnormal>"}}],
  "reviewed": [<every z index you reviewed>]
}}
"""


def parse_scan_response(raw: str, valid_z: set[int]) -> list[dict[str, Any]]:
    """Parse a scan reply into ``[{z:int, finding:str}]``, keeping only ``valid_z``."""
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
    sequences: list[str] | None = None,
) -> str:
    """Prompt for the reporting pass over the flagged (or fallback) level-montages.

    ``report_layout`` is the ordered image-by-image list of ``{"z", "window"}`` for the
    montages being sent, so the prompt can reference each by POSITION. ``flagged`` are the
    Pass-1 hints. When ``any_flagged`` is False these are representative levels.
    """
    context = f' The study is described as "{study_description}".' if study_description else ""
    report_z = sorted({int(e["z"]) for e in report_layout}, reverse=True)
    # Plane(s) present in this report batch (spine may send both sagittal and axial montages).
    planes_present = sorted({_plane_word(e.get("window")) for e in report_layout}) or ["axial"]
    plane = planes_present[0] if len(planes_present) == 1 else " and ".join(planes_present)
    legend = "\n".join(
        f"  Image {i}: {_plane_word(e.get('window'))} slice z={e['z']} (panel montage)"
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
                    "(hints — confirm from the images, they may misfire):\n" + hint_lines + "\n"
                )
        provenance = (
            f"The {len(report_z)} slice(s) below were FLAGGED as potentially "
            "abnormal while scanning the whole volume; report on them.\n" + hints
        )
    else:
        provenance = (
            f"A scan of the whole volume flagged NO focal abnormality. The {len(report_z)} "
            f"slice(s) below are representative slices sampled across the study; "
            "report accordingly (state plainly if there is no acute focal abnormality on them).\n"
        )

    return f"""You are a board-certified radiologist reading {REGION['exam_phrase']}.{context} \
No clinical history is assumed — assess systematically from the images alone.

{_panels(sequences, plane)}

{provenance}
You are shown {len(report_layout)} level-montage(s), in this exact order:
{legend}

Method:
- The flagged levels MAY BE FALSE POSITIVES. For each, re-examine across the sequences and decide \
whether it is TRULY abnormal. If it is a normal variant, artefact, or shine-through, state that the \
level is normal and do NOT report it as a finding; it is correct to conclude the study shows NO \
significant abnormality when that is the case — do not manufacture a diagnosis. BUT the reverse error \
is just as serious: do NOT dismiss or explain away a GENUINE, corroborated signal abnormality as \
"normal" or "age-related" — if a real abnormality is present, you MUST report and characterize it \
with an appropriate differential. {REGION['specificity_rules']}
- For each level, identify the anatomical level and the structures visible, then report any \
pathological finding you can actually SEE — {_TARGETS} — referencing the level (z) it appears \
on and CHARACTERIZING it by its signal across the sequences (which tiles it is bright/dark on, \
whether it enhances on T1+C or restricts on DWI if those tiles are present).
- Do NOT invent measurements, signal values, findings, or sequences that are not shown, and \
do not call a structure abnormal unless the tiles show it.
- Because only selected levels are shown, EXPLICITLY state the assessment is limited and that \
unshown levels were not reviewed.
- If the images are non-diagnostic or ambiguous, say so plainly rather than guessing.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<per-level findings across the shown levels — structure identification and any abnormality with its cross-sequence signal, referencing the level (z); 3-8 sentences>",
  "impression": "<concise overall impression and any recommended correlation/next step; 1-3 sentences>",
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def build_richread_prompt(
    image_zs: list[int],
    flagged: list[dict[str, Any]] | None,
    study_description: str | None,
    demographics: str | None = None,
    sequences: list[str] | None = None,
) -> str:
    """Detailed multiparametric read: characterize findings + systematic structure review."""
    context = f' The study is described as "{study_description}".' if study_description else ""
    if demographics and demographics.strip():
        _demo = demographics.strip()
        # Age is applied in this INTERPRETIVE pass for EVERY region (age changes what a finding
        # MEANS). Guard: only weigh age when an actual age is present (contains a digit); if it is
        # unknown, say so explicitly so the model never ASSUMES one (a real failure mode — it will
        # invent "under 50" from a rule and apply young-patient logic to anyone).
        if any(c.isdigit() for c in _demo):
            context += (
                f' Patient: {_demo}. Use the patient AGE and sex to INTERPRET every finding — weigh '
                'age-appropriate differentials and significance (a pattern that is expected or benign '
                'at one age can be abnormal at another), and do NOT dismiss OR over-call a finding on '
                'an age-inappropriate assumption. Use sex to name sex-appropriate organs.'
            )
        else:
            context += (
                f' Patient: {_demo} (age NOT stated — do NOT assume an age; use sex to name '
                'sex-appropriate organs).'
            )
    hint = ""
    if flagged:
        items = "; ".join(
            re.sub(r"^\s*\[[^\]]*\]\s*", "", str(f.get("finding", ""))).strip()
            for f in flagged if f.get("finding")
        )
        if items:
            hint = (
                f"\nA first pass flagged: {items}. These MAY BE FALSE POSITIVES — re-examine each "
                "across the sequences and CONFIRM or REFUTE it. If a flagged level is actually normal "
                "(variant, artefact, or shine-through), say so and do NOT report it. It is correct to "
                "conclude the study is normal if nothing is corroborated — but do NOT dismiss a "
                "GENUINE, corroborated abnormality (e.g. bilateral/confluent/cerebellar white-matter "
                "signal) as normal or age-related; a real abnormality MUST be reported.\n"
            )

    return f"""You are a consultant radiologist reading {REGION['exam_phrase']}.{context} You are \
shown {len(image_zs)} {REGION['richread_image_phrase']}.

{_panels(sequences, _plane_word(None))}
{hint}
{REGION['specificity_rules']}
Dictate a DETAILED FINDINGS section the way a radiologist writes a report:
- For each abnormality, describe it fully: its anatomical location, its SIGNAL on ONLY the \
sequences actually present as labeled tiles (do not describe signal on any sequence that is \
not shown), and its RELATIONSHIP to ADJACENT structures ({REGION['relationship_examples']}).
- Then give a SYSTEMATIC review of the structures visible: {REGION['organ_review']} — stating \
the status of each (normal, or the abnormality seen). Comment on a structure only if it is \
actually visible.
- GROUNDING: describe ONLY what is visible. NEVER state a numeric size or measurement — no \
centimetres, millimetres, or dimensions of ANY kind (you cannot measure from these images). \
You MAY describe size qualitatively ("bulky", "large", "small") but NEVER with a number. Do \
NOT invent signal values, sequences, or findings you cannot see. Do NOT mention slice/z \
numbers, "tiles", "panels", "montage", "images", or that an AI produced this — write as a \
radiologist dictating.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<detailed radiological FINDINGS: characterized abnormalities with cross-sequence signal and adjacent-structure relationships, then a systematic organ review; fluent prose>",
  "impression": "<concise impression of the significant findings + a brief hedged recommendation>",
  "disclaimer": "{_DISCLAIMER}"
}}
"""


def parse_report(raw: str) -> dict[str, str] | None:
    """Parse a report reply into ``{findings, impression, disclaimer}``."""
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


# A VLM cannot measure from a slice, so any numeric size it emits is fabricated. Strip
# measurement clauses as a safety net (the prompt also forbids them).
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
        logger.warning("mri_report_json_parse_failed", error=str(exc))
        return None
