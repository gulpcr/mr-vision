"""Two-stage, hybrid-local multi-agent engine for pan-abdominal CT screening.

Everything runs LOCAL — no data leaves the host. The two stages use DIFFERENT local
runtimes, chosen for what each does well:

    ┌─────────────────────────────────────────────────────────────────────────┐
    │ 0. Token-Aware Volumetric Batching                                        │
    │    ordered slice volume → overlapping blocks (default 40, 5-slice overlap)│
    ├─────────────────────────────────────────────────────────────────────────┤
    │ 1. STAGE 1 — Local Pathology Triage   (4B multimodal, HuggingFace)        │
    │    per block: prompt + N image slices → "STATUS: NORMAL"                  │
    │              | "STATUS: ABNORMAL | LOCAL_INDICES:[s,e] | REASON:..."      │
    ├─────────────────────────────────────────────────────────────────────────┤
    │ 2. Coordination Layer                                                     │
    │    parse (fail-safe → ABNORMAL) → map local→absolute indices →            │
    │    interval-merge overlapping abnormal spans into pathology zones         │
    ├─────────────────────────────────────────────────────────────────────────┤
    │ 3. STAGE 2 — Targeted Clinical Reporting   (27B text-only, Ollama)        │
    │    per zone: block findings + running history → inline reasoning +        │
    │    synthesized report; the last zone emits the single unified report      │
    └─────────────────────────────────────────────────────────────────────────┘

Design intent
-------------
* **Right runtime per stage.** The engine holds NO model weights itself; it takes two
  injected clients with the same async surface (``.ready`` + ``async
  generate_from_images`` + ``async generate_text``). The task hook wires Stage 1 to an
  :class:`~app.infrastructure.llm.hf_medgemma_client.HFMedGemmaVisionClient` (HF, because
  Ollama's /api/generate cannot template multi-image prompts for the Gemma-3 vision
  stack) and Stage 2 to the Ollama :class:`~app.infrastructure.llm.medgemma_client.MedGemmaClient`
  (text-only — no reason to hold the 27B in the worker's VRAM). Because the interface is
  identical, the engine is runtime-agnostic and either client may be swapped/mocked.
* **Non-blocking by contract.** Each client returns ``""`` on any failure and the parsers
  degrade to empty/fail-safe, so a model outage yields an empty result rather than
  raising — the deterministic postprocess() summary survives.
* **Pan-abdominal, segmentation-free.** No organ-specific logic; Stage 1 flags *any*
  abnormality anywhere in the block.
* **Sensitivity-first / fail-safe.** A malformed or unreadable Stage 1 reply is treated
  as ABNORMAL for the whole block, so parsing failures never silently drop pathology.

For standalone runs / tests without any model, :class:`MockMedGemmaClient` provides a
deterministic drop-in so the whole pipeline is runnable end-to-end (see ``__main__``).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

import structlog

logger = structlog.get_logger(__name__)


# ── Prompt templates (verbatim contracts the models must satisfy) ────────────────

# Stage 1 uses two named fields only ({batch_length}, {last_index}) so the template is
# a valid ``str.format`` string — ``{batch_length-1}`` would not be.
_STAGE1_TRIAGE_PROMPT = """\
You are a high-speed radiology triage agent analyzing a sequential 3D block of \
{batch_length} CT slices from an abdominal scan. The relative slice index of these \
images runs from 0 to {last_index}.

Scan every single slice for any visible pathology, structural distortion, abnormal \
fluid pocket, mass effect, or focal lesion anywhere in this block.

CRITICAL OUTPUT RULES — follow them exactly:
- Your ENTIRE reply must be a SINGLE line that BEGINS with the word "STATUS:".
- Do NOT write a "FINDINGS" paragraph, an impression, or ANY prose before the STATUS line.
- If the block is completely normal, output exactly this and nothing else:
STATUS: NORMAL
- If you detect ANY abnormal finding, output exactly this form and nothing else:
STATUS: ABNORMAL | LOCAL_INDICES: [start_index, end_index] | REASON: [one brief clinical phrase]
"""

# Stage 2 keeps the caller-supplied contract and adds a {block_findings} slot: the
# text-only report model cannot see the images, so it needs this block's own Stage 1
# findings as text, alongside the {running_history} accumulated from superior blocks.
_STAGE2_REPORT_PROMPT = """\
You are an expert abdominal radiologist. You are analyzing text findings and summaries \
from a targeted segment of an abdominal CT scan showing suspected pathology \
(Absolute Slices {start} to {end}).

Findings flagged in THIS block by the triage agent:
{block_findings}

Here is the running medical history and findings flagged from superior blocks of this \
scan: {running_history}

Output Format:
**FLAGGED**: YES
**INLINE CLINICAL REASONING**: [Your clinical analysis of this specific block's findings]
**SYNTHESIZED RADIOLOGICAL REPORT**: [Generate the updated full clinical report findings, synthesizing this block with the running history]
"""

_NO_HISTORY_SENTINEL = "(none — this is the most superior flagged block)"
_NO_FINDINGS_SENTINEL = "(triage returned no specific phrase; treat as indeterminate abnormality)"


# ── Structured results ───────────────────────────────────────────────────────────

@dataclass
class SliceBatch:
    """One token-aware volumetric block handed to the Stage 1 model.

    ``absolute_start`` is the master-array index of the block's slice 0, so a local
    index ``i`` reported by the model maps to ``absolute_start + i``. ``slices`` holds
    the raw PNG bytes of each slice (what the multimodal client sends).
    """

    batch_id: int
    absolute_start: int
    slices: list[bytes]

    @property
    def length(self) -> int:
        return len(self.slices)

    @property
    def absolute_end(self) -> int:
        """Inclusive absolute index of the last slice in the block."""
        return self.absolute_start + self.length - 1


@dataclass
class TriageResult:
    """Parsed Stage 1 verdict for a single block."""

    batch_id: int
    is_abnormal: bool
    absolute_start: int  # abnormal span start (inclusive), absolute index
    absolute_end: int    # abnormal span end   (inclusive), absolute index
    reason: str
    malformed: bool = False   # True → verdict came from the fail-safe path
    raw_response: str = ""


@dataclass
class PathologyZone:
    """A consolidated, de-duplicated abnormal span across the master volume."""

    absolute_start: int
    absolute_end: int
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"slices {self.absolute_start}-{self.absolute_end}"


@dataclass
class StageTwoBlock:
    """Stage 2 output for one pathology zone."""

    zone: PathologyZone
    inline_reasoning: str
    synthesized_report: str
    raw_response: str = ""


@dataclass
class PipelineReport:
    """Everything the pipeline produced, for persistence / UI / audit."""

    normal: bool
    final_report: str
    total_slices: int
    num_batches: int
    triage_results: list[TriageResult]
    pathology_zones: list[PathologyZone]
    stage_two_blocks: list[StageTwoBlock]

    def to_dict(self) -> dict[str, Any]:
        """Flat, JSON-serialisable view."""
        return {
            "normal": self.normal,
            "final_report": self.final_report,
            "total_slices": self.total_slices,
            "num_batches": self.num_batches,
            "pathology_zones": [
                {"start": z.absolute_start, "end": z.absolute_end, "reasons": z.reasons}
                for z in self.pathology_zones
            ],
            "triage": [
                {
                    "batch_id": t.batch_id,
                    "abnormal": t.is_abnormal,
                    "start": t.absolute_start,
                    "end": t.absolute_end,
                    "reason": t.reason,
                    "malformed": t.malformed,
                }
                for t in self.triage_results
            ],
            "stage_two": [
                {
                    "zone": b.zone.label,
                    "inline_reasoning": b.inline_reasoning,
                    "synthesized_report": b.synthesized_report,
                }
                for b in self.stage_two_blocks
            ],
        }


# ── The engine ───────────────────────────────────────────────────────────────────

class CTAbdomen4Pipeline:
    """Two-stage, two-model local multi-agent pan-abdominal CT screening engine.

    Parameters
    ----------
    triage_client :
        Stage 1 client (4B multimodal). Must expose ``ready`` and
        ``async generate_from_images(prompt, images: list[bytes])`` — the task hook passes
        an :class:`app.infrastructure.llm.hf_medgemma_client.HFMedGemmaVisionClient`.
    report_client :
        Stage 2 client (27B text). Must expose ``ready`` and
        ``async generate_text(prompt)`` — the task hook passes an Ollama
        :class:`app.infrastructure.llm.medgemma_client.MedGemmaClient`. May be the SAME
        object as ``triage_client`` (e.g. the mock).
    batch_size :
        Slices per Stage 1 inference call (Token-Aware Volumetric Batching).
    overlap :
        Slices shared between consecutive blocks. Block stride = ``batch_size - overlap``
        (e.g. size 40 / overlap 5 → block 0 = slices 0..39, block 1 = 35..74, …).
    merge_gap :
        Absolute-index gap up to which two abnormal spans are still merged into one
        pathology zone. 0 = merge only truly overlapping/adjacent spans.

    Notes
    -----
    40-image triage blocks work because Stage 1 runs in HuggingFace, whose processor
    templates all N image tokens correctly and whose context is large. The engine itself
    is runtime-agnostic — it just batches whatever size it is given and calls the client.
    """

    def __init__(
        self,
        triage_client: Any,
        report_client: Any,
        *,
        batch_size: int = 40,
        overlap: int = 5,
        merge_gap: int = 0,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if not 0 <= overlap < batch_size:
            raise ValueError("overlap must satisfy 0 <= overlap < batch_size")
        if merge_gap < 0:
            raise ValueError("merge_gap must be >= 0")

        self.triage_client = triage_client
        self.report_client = report_client
        self.batch_size = batch_size
        self.overlap = overlap
        self.stride = batch_size - overlap
        self.merge_gap = merge_gap

    # ── public entry point ───────────────────────────────────────────────────────

    async def run(self, slices: Sequence[bytes]) -> PipelineReport:
        """Screen an ordered slice volume (PNG bytes) and return a :class:`PipelineReport`.

        ``slices`` is the ordered stack of rendered slice PNGs (superior→inferior), one
        ``bytes`` per slice — exactly what the task hook reads off disk.
        """
        total = len(slices)
        if total == 0:
            raise ValueError("run() requires a non-empty ordered slice volume")

        batches = self._build_batches(list(slices))
        logger.info(
            "abdomen_ct4_batched",
            total_slices=total, num_batches=len(batches),
            batch_size=self.batch_size, overlap=self.overlap, stride=self.stride,
        )

        # Stage 1 — sequential triage over the volumetric blocks (one Ollama call each).
        triage_results = [await self._triage_batch(b) for b in batches]

        # Coordination — map to absolute indices and merge into pathology zones.
        zones = self._coordinate_zones(triage_results)
        logger.info(
            "abdomen_ct4_zones",
            abnormal_batches=sum(t.is_abnormal for t in triage_results),
            malformed_batches=sum(t.malformed for t in triage_results),
            pathology_zones=len(zones),
        )

        # Stage 2 — targeted reporting with a running clinical history.
        if not zones:
            return PipelineReport(
                normal=True,
                final_report=_NORMAL_REPORT,
                total_slices=total,
                num_batches=len(batches),
                triage_results=triage_results,
                pathology_zones=[],
                stage_two_blocks=[],
            )

        stage_two_blocks, final_report = await self._generate_report(zones)
        return PipelineReport(
            normal=False,
            final_report=final_report,
            total_slices=total,
            num_batches=len(batches),
            triage_results=triage_results,
            pathology_zones=zones,
            stage_two_blocks=stage_two_blocks,
        )

    # ── 0. Token-Aware Volumetric Batching ─────────────────────────────────────────

    def _build_batches(self, slices: list[bytes]) -> list[SliceBatch]:
        """Slice the volume into overlapping ``batch_size`` blocks with ``overlap`` shared.

        Blocks advance by ``stride = batch_size - overlap``. The final block is emitted
        even if short, and once a block reaches the end no further (fully-overlapping)
        block is produced.
        """
        batches: list[SliceBatch] = []
        start = 0
        n = len(slices)
        while start < n:
            block = slices[start:start + self.batch_size]
            batches.append(SliceBatch(batch_id=len(batches), absolute_start=start, slices=block))
            if start + self.batch_size >= n:  # reached the end — stop, no redundant tail
                break
            start += self.stride
        return batches

    # ── 1. Stage 1: local pathology triage ─────────────────────────────────────────

    async def _triage_batch(self, batch: SliceBatch) -> TriageResult:
        """Run the multimodal client on one block and parse its verdict (fail-safe)."""
        prompt = _STAGE1_TRIAGE_PROMPT.format(
            batch_length=batch.length, last_index=max(0, batch.length - 1)
        )
        try:
            raw = await self.triage_client.generate_from_images(prompt, batch.slices)
        except Exception as exc:
            # A hard inference failure is treated as ABNORMAL for the whole block so we
            # never drop a block on an infra hiccup (sensitivity-first).
            logger.warning("abdomen_ct4_triage_inference_error", batch_id=batch.batch_id, error=str(exc))
            return self._failsafe_abnormal(batch, reason="triage inference error", raw="")

        result = self._parse_triage(raw, batch)
        logger.info(
            "abdomen_ct4_triage",
            batch_id=batch.batch_id,
            abs_range=[batch.absolute_start, batch.absolute_end],
            abnormal=result.is_abnormal, malformed=result.malformed,
            span=[result.absolute_start, result.absolute_end] if result.is_abnormal else None,
            # Raw model reply (truncated) so triage behaviour can be inspected/tuned —
            # e.g. distinguishing a genuine "STATUS: NORMAL" from under-attention.
            raw=(result.raw_response or "")[:200],
        )
        return result

    def _parse_triage(self, raw: str, batch: SliceBatch) -> TriageResult:
        """Parse a Stage 1 reply with regex, protected by defensive fallbacks.

        Recognised replies::

            STATUS: NORMAL
            STATUS: ABNORMAL | LOCAL_INDICES: [s, e] | REASON: <phrase>

        Any unrecognised / empty / malformed reply → fail-safe ABNORMAL over the whole
        block, so parsing failures cannot hide pathology.
        """
        text = (raw or "").strip()
        if not text:
            return self._failsafe_abnormal(batch, reason="empty triage response", raw=raw)

        try:
            status_match = re.search(r"STATUS\s*:\s*(NORMAL|ABNORMAL)", text, re.IGNORECASE)
            if status_match is None:
                return self._failsafe_abnormal(batch, reason="no STATUS token in response", raw=raw)

            status = status_match.group(1).upper()
            if status == "NORMAL":
                return TriageResult(
                    batch_id=batch.batch_id, is_abnormal=False,
                    absolute_start=batch.absolute_start, absolute_end=batch.absolute_end,
                    reason="normal", raw_response=text,
                )

            # ABNORMAL: pull the local index span and the reason phrase.
            local_start, local_end = self._parse_local_indices(text, batch.length)
            reason = self._parse_reason(text)

            abs_start = batch.absolute_start + local_start
            abs_end = batch.absolute_start + local_end
            return TriageResult(
                batch_id=batch.batch_id, is_abnormal=True,
                absolute_start=abs_start, absolute_end=abs_end,
                reason=reason, raw_response=text,
            )
        except Exception as exc:  # any unexpected parse error → fail safe
            logger.warning("abdomen_ct4_triage_parse_error", batch_id=batch.batch_id, error=str(exc))
            return self._failsafe_abnormal(batch, reason="malformed triage response", raw=raw)

    @staticmethod
    def _parse_local_indices(text: str, batch_length: int) -> tuple[int, int]:
        """Extract ``[start, end]`` local indices, clamped to the block; widest span on miss.

        If indices are missing/garbled we default to the whole block (0..len-1) rather
        than guessing a narrow span — again, sensitivity-first.
        """
        last = max(0, batch_length - 1)
        match = re.search(
            r"LOCAL_INDICES\s*:\s*\[?\s*(\d+)\s*[,\-]\s*(\d+)\s*\]?", text, re.IGNORECASE
        )
        if match is None:
            # Fall back to any two integers near an index-ish token, else whole block.
            nums = re.findall(r"\d+", text)
            if len(nums) >= 2:
                a, b = int(nums[0]), int(nums[1])
            else:
                return 0, last
        else:
            a, b = int(match.group(1)), int(match.group(2))

        lo, hi = min(a, b), max(a, b)
        lo = max(0, min(lo, last))
        hi = max(0, min(hi, last))
        return lo, hi

    @staticmethod
    def _parse_reason(text: str) -> str:
        """Extract the REASON phrase; empty string if absent."""
        match = re.search(r"REASON\s*:\s*\[?\s*(.+?)\s*\]?\s*$", text, re.IGNORECASE | re.DOTALL)
        if match is None:
            return ""
        # Collapse whitespace/newlines into a single clinical phrase.
        return re.sub(r"\s+", " ", match.group(1)).strip()

    @staticmethod
    def _failsafe_abnormal(batch: SliceBatch, *, reason: str, raw: str) -> TriageResult:
        """Mark the entire block abnormal — the safe default when triage can't be trusted."""
        return TriageResult(
            batch_id=batch.batch_id, is_abnormal=True,
            absolute_start=batch.absolute_start, absolute_end=batch.absolute_end,
            reason=f"[FAIL-SAFE] {reason}", malformed=True, raw_response=raw,
        )

    # ── 2. Coordination: absolute mapping + interval merge ─────────────────────────

    def _coordinate_zones(self, triage_results: list[TriageResult]) -> list[PathologyZone]:
        """Merge overlapping abnormal absolute spans into consolidated pathology zones."""
        spans = [
            (t.absolute_start, t.absolute_end, t.reason)
            for t in triage_results
            if t.is_abnormal
        ]
        if not spans:
            return []

        spans.sort(key=lambda s: (s[0], s[1]))
        zones: list[PathologyZone] = []
        cur = PathologyZone(spans[0][0], spans[0][1], self._collect_reason([], spans[0][2]))

        for start, end, reason in spans[1:]:
            if start <= cur.absolute_end + self.merge_gap:  # overlap / within gap → merge
                cur.absolute_end = max(cur.absolute_end, end)
                cur.reasons = self._collect_reason(cur.reasons, reason)
            else:
                zones.append(cur)
                cur = PathologyZone(start, end, self._collect_reason([], reason))
        zones.append(cur)
        return zones

    @staticmethod
    def _collect_reason(existing: list[str], reason: str) -> list[str]:
        """Append a de-duplicated, non-empty reason phrase."""
        reason = (reason or "").strip()
        if reason and reason not in existing:
            return [*existing, reason]
        return list(existing)

    # ── 3. Stage 2: targeted clinical reporting with running history ───────────────

    async def _generate_report(self, zones: list[PathologyZone]) -> tuple[list[StageTwoBlock], str]:
        """Walk the pathology zones superior→inferior, threading a running history.

        Each zone's synthesized report becomes the running history fed into the next
        zone's prompt, so the final zone emits a single unified report rather than one
        fragment per zone.
        """
        blocks: list[StageTwoBlock] = []
        running_history = ""

        for zone in zones:
            block_findings = "; ".join(zone.reasons) if zone.reasons else _NO_FINDINGS_SENTINEL
            prompt = _STAGE2_REPORT_PROMPT.format(
                start=zone.absolute_start,
                end=zone.absolute_end,
                block_findings=block_findings,
                running_history=running_history.strip() or _NO_HISTORY_SENTINEL,
            )

            try:
                raw = await self.report_client.generate_text(prompt)
            except Exception as exc:
                logger.warning("abdomen_ct4_report_inference_error", zone=zone.label, error=str(exc))
                raw = ""

            inline, synthesized = self._parse_report(raw, zone, block_findings)
            blocks.append(StageTwoBlock(zone=zone, inline_reasoning=inline,
                                        synthesized_report=synthesized, raw_response=raw))
            # The synthesized report (which already folds in prior history) becomes the
            # new running history for the next block.
            running_history = synthesized or running_history
            logger.info(
                "abdomen_ct4_report_block",
                zone=zone.label, has_report=bool(synthesized), history_chars=len(running_history),
            )

        final_report = blocks[-1].synthesized_report if blocks else ""
        if not final_report:
            # Degrade gracefully: stitch whatever we have rather than returning nothing.
            final_report = "\n\n".join(
                f"{b.zone.label}: {b.synthesized_report or b.inline_reasoning or '; '.join(b.zone.reasons)}"
                for b in blocks
            ).strip() or _DEGRADED_REPORT
        return blocks, final_report

    @staticmethod
    def _parse_report(raw: str, zone: PathologyZone, block_findings: str) -> tuple[str, str]:
        """Parse the Stage 2 reply's INLINE REASONING and SYNTHESIZED REPORT sections.

        On a malformed/empty reply we degrade to the block's own triage findings so the
        running history still carries the pathology forward.
        """
        text = (raw or "").strip()
        fallback_inline = f"Triage-flagged abnormality at {zone.label}: {block_findings}"

        if not text:
            return fallback_inline, ""

        try:
            inline = _extract_section(
                text, r"INLINE CLINICAL REASONING", next_labels=(r"SYNTHESIZED RADIOLOGICAL REPORT",)
            )
            synthesized = _extract_section(text, r"SYNTHESIZED RADIOLOGICAL REPORT", next_labels=())
            return inline or fallback_inline, synthesized
        except Exception as exc:  # never let a parse error break the walk
            logger.warning("abdomen_ct4_report_parse_error", zone=zone.label, error=str(exc))
            return fallback_inline, ""


# ── section extraction helper ────────────────────────────────────────────────────

def _extract_section(text: str, label: str, next_labels: tuple[str, ...]) -> str:
    """Return the text following ``**label**:`` up to the next known section (or EOS).

    Tolerates optional ``**`` bolding and ``:`` after the label.
    """
    start = re.search(rf"\*{{0,2}}\s*{label}\s*\*{{0,2}}\s*:?", text, re.IGNORECASE)
    if start is None:
        return ""
    body = text[start.end():]
    # Trim at the earliest following section label, if any.
    ends = [
        m.start()
        for lbl in next_labels
        for m in [re.search(rf"\*{{0,2}}\s*{lbl}\s*\*{{0,2}}\s*:?", body, re.IGNORECASE)]
        if m is not None
    ]
    if ends:
        body = body[: min(ends)]
    return re.sub(r"\s+", " ", body).strip()


# ── canned reports for edge cases ────────────────────────────────────────────────

_NORMAL_REPORT = (
    "FINDINGS: No focal pan-abdominal abnormality was flagged by the triage agent across "
    "the reviewed volume. No mass effect, abnormal fluid collection, or gross structural "
    "distortion identified on the screened slices.\n\n"
    "IMPRESSION: No acute focal abnormality on this AI screening pass. NON-DIAGNOSTIC "
    "assistive output — full-volume radiologist review remains required."
)

_DEGRADED_REPORT = (
    "IMPRESSION: Suspected abnormality was flagged but the reporting model returned no "
    "usable narrative. Manual radiologist review of the flagged slices is required. "
    "NON-DIAGNOSTIC assistive output."
)


# ── deterministic mock client (standalone / tests without Ollama) ────────────────

class MockMedGemmaClient:
    """Deterministic, MedGemmaClient-compatible stub so the engine runs without Ollama.

    ``generate_from_images`` returns ABNORMAL for larger blocks and NORMAL for small
    tail blocks (no randomness → reproducible). ``generate_text`` echoes the block/
    running-history context in the Stage 2 output format.
    """

    ready = True

    async def generate_from_images(self, prompt: str, images: Sequence[bytes] | None) -> str:
        n = len(images or [])
        if n >= 6:
            end = max(0, min(2, n - 1))
            return (
                "STATUS: ABNORMAL | LOCAL_INDICES: [1, %d] | "
                "REASON: mock — suspected focal soft-tissue lesion" % end
            )
        return "STATUS: NORMAL"

    async def generate_text(self, prompt: str) -> str:
        m = re.search(r"Absolute Slices\s+(\d+)\s+to\s+(\d+)", prompt)
        span = f"slices {m.group(1)}-{m.group(2)}" if m else "the flagged segment"
        return (
            "**FLAGGED**: YES\n"
            f"**INLINE CLINICAL REASONING**: Mock analysis of {span}: the triage-flagged "
            "focal abnormality is consistent with a soft-tissue lesion; correlation advised.\n"
            f"**SYNTHESIZED RADIOLOGICAL REPORT**: FINDINGS: Focal abnormality identified at "
            f"{span}, integrated with superior findings. IMPRESSION: Suspected pan-abdominal "
            "pathology — recommend radiologist review and dedicated imaging. NON-DIAGNOSTIC "
            "assistive output."
        )


if __name__ == "__main__":  # pragma: no cover — standalone smoke test (mock client)
    # Runs end-to-end with no Ollama: 60 dummy slice "PNGs" → batches → triage → report.
    import json

    client = MockMedGemmaClient()
    pipe = CTAbdomen4Pipeline(client, client, batch_size=8, overlap=2)
    dummy_volume = [f"slice_{i}".encode() for i in range(60)]
    report = asyncio.run(pipe.run(dummy_volume))
    print(json.dumps(report.to_dict(), indent=2))
