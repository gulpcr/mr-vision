from __future__ import annotations

"""Report-writer for abdomen CT screening results.

This is a SEPARATE, post-hoc step from the screening pipeline: it takes the per-level
findings a pipeline already produced (``[{z, finding}]``) and asks a TEXT model to
reorganize them into a clean, grouped FINDINGS / CONCLUSIONS report. It does NOT change
how the pipeline flags slices, and it is strictly grounded — the prompt forbids the model
from adding, inferring, or inventing any finding not in the input. Fully non-blocking:
returns ``None`` on any failure so the caller can fall back to the raw findings.

The model is injected (any client exposing ``ready`` and ``async generate_text(prompt)``
— MedGemmaClient or GeminiClient), so the writer model is decoupled from the pipeline.
"""

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class AbdomenReportService:
    """Consolidates per-level screening flags into a grounded findings/conclusions report."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "ready", False))

    async def consolidate(
        self,
        *,
        flagged: list[dict[str, Any]],
        study_description: str | None = None,
        detail: str | None = None,
    ) -> dict[str, str] | None:
        """Return ``{findings, conclusions}`` written from the inputs only, or None.

        ``flagged`` is ``[{z, finding}]`` (each finding may carry a leading ``[window]``
        tag) — the authoritative list of abnormalities. ``detail`` is the pipeline's own
        per-slice narrative (``summary.ai_report.findings``), an OPTIONAL richer source
        the writer may draw fuller descriptions from — but it is still grounding, not a
        licence to invent. The model is told these are its only sources of truth.
        """
        if not self.available:
            return None
        prompt = self._build_prompt(flagged, study_description, detail)
        try:
            raw = await self._client.generate_text(prompt)
        except Exception as exc:  # never propagate — report writing is best-effort
            logger.warning("abdomen_report_generate_failed", error=str(exc))
            return None
        parsed = self._parse(raw)
        if parsed:
            parsed["conclusions"] = self._ensure_tumour_marker_advice(
                parsed.get("findings", ""), parsed.get("conclusions", "")
            )
        return parsed

    # A mass/tumour warrants tumour-marker correlation; guarantee that line when the
    # findings describe one and name the SITE-SPECIFIC serum marker where possible.
    # NOTE: tumour markers are BLOOD tests (not visible on CT) — we RECOMMEND them,
    # we never report a value.
    _TUMOUR_RE = re.compile(
        r"\b(mass|tumou?r|neoplas|malignan|carcinoma|metasta|adnexal)\b", re.IGNORECASE
    )
    # Site → serum marker, matched within a sentence that describes a mass/tumour.
    _SITE_MARKERS = [
        (re.compile(r"adnex|ovar|fallopian|\bpelvic\b", re.IGNORECASE), "CA-125"),
        (re.compile(r"hepat|\bliver\b", re.IGNORECASE), "AFP"),
        (re.compile(r"pancrea", re.IGNORECASE), "CA 19-9"),
        (re.compile(r"colon|colorect|rectal|rectum|sigmoid|caec|\bbowel\b", re.IGNORECASE), "CEA"),
        (re.compile(r"testic", re.IGNORECASE), "AFP and beta-hCG"),
    ]
    _GENERIC_MARKER_RE = re.compile(
        r"(?:relevant\s+|serum\s+|appropriate\s+)?tumou?r\s*marker(?:\s*assessment)?s?",
        re.IGNORECASE,
    )

    @classmethod
    def _marker_for(cls, findings: str) -> str | None:
        """The site-specific serum marker for the described mass, or None if unclear."""
        for sent in re.split(r"(?<=[.!?])\s+", findings or ""):
            if not cls._TUMOUR_RE.search(sent):
                continue  # only weigh sentences that actually describe a mass/tumour
            for rx, marker in cls._SITE_MARKERS:
                if rx.search(sent):
                    return marker
        return None

    @classmethod
    def _ensure_tumour_marker_advice(cls, findings: str, conclusions: str) -> str:
        if not cls._TUMOUR_RE.search(findings or ""):
            return conclusions
        c = conclusions or ""
        marker = cls._marker_for(findings)
        if marker:
            if re.search(re.escape(marker), c, re.IGNORECASE):
                return c  # already names the specific marker
            # Upgrade a generic "tumour markers" mention to the specific marker.
            c2, n = cls._GENERIC_MARKER_RE.subn(f"serum {marker}", c, count=1)
            if n:
                return c2
            advice = f"Correlation with serum {marker} is recommended."
        else:
            if cls._GENERIC_MARKER_RE.search(c):
                return c
            advice = "Correlation with relevant serum tumour markers is recommended."
        c = c.rstrip()
        if c and not c.endswith((".", "!", "?")):
            c += "."
        return (c + " " + advice).strip() if c else advice

    # ── prompt / parse ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_prompt(
        flagged: list[dict[str, Any]],
        study_description: str | None,
        detail: str | None = None,
    ) -> str:
        context = f' The study is described as "{study_description}".' if study_description else ""

        # Polish mode: when a detailed read is available it IS the observations — write it
        # up faithfully (findings incl. the systematic organ review) + a hedged impression.
        if detail and detail.strip():
            return f"""You are an experienced consultant radiologist finalizing the FINDINGS and \
CONCLUSIONS of an ABDOMEN/PELVIS CT report.{context}

Below is your detailed read of the study — these are the observations to write up:
\"\"\"
{detail.strip()}
\"\"\"

Rewrite these observations into a polished report:
- FINDINGS: fluent radiologist prose in full sentences. PRESERVE ALL the observations above, \
including the systematic organ review and any normal-organ or incidental statements (e.g. \
"The liver shows diffuse fatty infiltration with no focal lesion", "No abdominopelvic \
lymphadenopathy", "Visualized bones show degenerative changes"). Describe each abnormality at \
its anatomical location with the relationships already noted.
- Do NOT mention slice/z numbers, "images", or that an AI produced this — write as a radiologist \
dictating the study.
- Do NOT add any finding, measurement, size, density, or organ comment that is NOT in the \
observations above, and do NOT drop any stated finding.
- CONCLUSIONS: a concise impression of the significant findings plus a brief, clearly-HEDGED \
interpretation and recommendation that follows from them (e.g. "...concerning for recurrent/ \
residual disease; recommend gynaecological correlation and tumour markers"). Keep it hedged; do \
not assert a definitive diagnosis.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<the observations rewritten as a fluent FINDINGS section; abnormalities characterized with relationships, then the organ review; NO slice/z or AI references>",
  "conclusions": "<concise impression of the significant findings + a brief hedged recommendation; radiologist voice>"
}}
"""

        detail_block = ""
        if flagged:
            # z is provided only so you can tell which observations are the same finding
            # repeated on adjacent slices — it is INTERNAL and must NEVER appear in the report.
            lines = "\n".join(
                f"  (slice {f.get('z')}) {AbdomenReportService._clean(str(f.get('finding', '')))}"
                for f in flagged if f.get("finding")
            ) or "  (an abnormality was flagged without a specific description)"
            body = (
                "A screening pass produced the following raw observations (the slice number in "
                "parentheses is internal bookkeeping only):\n"
                f"{lines}\n\n"
                "Rewrite these as the FINDINGS and CONCLUSIONS of a radiology report, dictated the "
                "way an experienced abdominal radiologist would:\n"
                "- Write fluent, natural clinical prose in full sentences — a flowing narrative, "
                "NOT a list. Use standard radiological phrasing and describe each finding at its "
                "anatomical location (e.g. \"There is a soft-tissue mass in the right adnexa. "
                "Moderate free fluid is present within the abdomen and pelvis. Right-sided "
                "hydronephrosis is noted.\").\n"
                "- NEVER mention slice numbers, z-indices, 'levels', 'images', or that an AI/model "
                "produced this — write as a radiologist reading the study.\n"
                "- State each distinct finding ONCE; the same finding seen on several slices is a "
                "single finding — describe its anatomical extent in words, not slice ranges.\n"
                "- GROUNDING (critical): in FINDINGS describe ONLY the abnormalities listed "
                "above. You may phrase them in proper radiological language and give their "
                "anatomical location, but you must NOT invent sizes, measurements, HU/densities, "
                "or any additional finding that is not in the list. Do not comment on organs not "
                "mentioned above and do not state that other structures are normal.\n"
                "- In CONCLUSIONS, write like a radiologist: briefly synthesize the findings and "
                "add a short, clearly-HEDGED clinical impression and recommendation that FOLLOWS "
                "from them (e.g. \"...concerning for an ovarian neoplasm; recommend gynaecological "
                "correlation and tumour markers\"). This interpretive impression is expected and is "
                "NOT a new finding — but keep it hedged (\"suspicious for\", \"may represent\") and "
                "do not assert a definitive diagnosis."
            )
        else:
            body = (
                "The screening pass identified no focal abnormality. Write a brief FINDINGS "
                "paragraph in natural radiological prose stating that no significant focal "
                "abnormality is identified in the abdomen and pelvis, and a matching CONCLUSIONS "
                "line. Do NOT mention slices, images, or an AI, and do not invent any finding."
            )

        return f"""You are an experienced consultant radiologist dictating the FINDINGS and \
CONCLUSIONS of an ABDOMEN/PELVIS CT report.{context}

{body}{detail_block}

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{{
  "findings": "<fluent radiological narrative in full sentences; each finding described at its anatomical location; NO slice/z references, NO AI references>",
  "conclusions": "<a concise numbered or prose impression of the significant findings, ending with a brief correlation/next-step recommendation; radiologist voice>"
}}
"""

    @staticmethod
    def _parse(raw: str) -> dict[str, str] | None:
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
            logger.warning("abdomen_report_parse_failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        findings = AbdomenReportService._strip_measurements(str(data.get("findings", "") or "").strip())
        conclusions = AbdomenReportService._strip_measurements(
            str(data.get("conclusions", "") or data.get("impression", "") or "").strip()
        )
        if not findings and not conclusions:
            return None
        return {"findings": findings, "conclusions": conclusions}

    # A VLM cannot measure from a slice — any numeric size is fabricated; strip it.
    _MEASURE_RE = re.compile(
        r"[,;]?\s*(?:,?\s*(?:which is\s+|and\s+)?measuring\s+)?"
        r"(?:approximately\s+|approx\.?\s+|about\s+|roughly\s+|~\s*)?"
        r"\d+(?:\.\d+)?\s*(?:[-–xX×]\s*\d+(?:\.\d+)?\s*)*(?:mm|cm)\b"
        r"(?:\s+in\s+(?:its\s+)?[a-z ]+?dimension)?",
        re.IGNORECASE,
    )

    @staticmethod
    def _strip_measurements(text: str) -> str:
        if not text:
            return text
        t = AbdomenReportService._MEASURE_RE.sub("", text)
        t = re.sub(r"\s{2,}", " ", t)
        t = re.sub(r"\s+([.,;])", r"\1", t)
        return t.strip()

    @staticmethod
    def _clean(s: str) -> str:
        """Strip the leading ``[window]`` provenance tag from a flag description."""
        return re.sub(r"^\s*\[[^\]]*\]\s*", "", s).strip()
