"""Single-pass MedGemma orchestration for abdomen_ct3.

:func:`scan_and_synthesize` runs the whole flow against an already-constructed MedGemma
client (dependency-injected by the Celery task hook — this module imports no
infrastructure and does no file I/O):

  Scan:      the per-slice scan images (per scan window) are chunked into LARGE batches
             of ``batch_size`` and sent to MedGemma; each reply flags abnormal z-levels
             WITH a reason. Flags accumulate across batches and windows (tagged by the
             window they were seen in).
  Synthesis: the accumulated flag reasons are composed into a structured
             findings/impression report in ONE TEXT-ONLY call — no images are re-sent
             (MedGemma already saw every slice during the scan).

Fully non-blocking by contract: the injected client returns "" on any failure and the
parsers degrade to empty/None, so a MedGemma outage yields an empty result rather than
raising. Returns ``{anomaly_z, flagged, report_z, any_flagged, ai_report, batches}``.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.usecases.abdomen_ct3 import prompts

logger = structlog.get_logger(__name__)


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


def _even_pick(items: list[int], k: int) -> list[int]:
    """Evenly-spread subset of ``items`` (order preserved), at most ``k`` elements."""
    if k <= 0 or not items:
        return []
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    seen: list[int] = []
    for i in range(k):
        v = items[round(i * step)]
        if v not in seen:
            seen.append(v)
    return seen


async def scan_and_synthesize(
    *,
    client: Any,
    scan_images_by_window: dict[str, list[dict[str, Any]]],
    study_description: str | None,
    batch_size: int = 10,
    max_report_levels: int = 24,
) -> dict[str, Any]:
    """Scan every slice in large batches, then synthesize a report from the flags.

    ``scan_images_by_window``: ``{window: [{"z", "name", "bytes"}]}`` — one image per
      candidate slice per SCAN window, superior→inferior. Each window is scanned over
      the whole volume independently and the flagged levels are unioned.
    """
    scan_windows = [w for w, imgs in scan_images_by_window.items() if imgs]
    if not scan_windows:
        return {"anomaly_z": [], "flagged": [], "report_z": [], "any_flagged": False,
                "ai_report": None, "batches": 0}

    # ── Scan: large batches per window, flag-with-reason, union across windows ──
    flagged: dict[int, str] = {}          # z → best finding text (any window)
    all_z: set[int] = set()
    total_batches = 0
    for window in scan_windows:
        imgs = scan_images_by_window[window]
        all_z.update(int(e["z"]) for e in imgs)
        batches = _chunk(imgs, batch_size)
        total_batches += len(batches)
        for i, batch in enumerate(batches, 1):
            prompt = prompts.build_scan_prompt(
                batch, study_description=study_description, window=window
            )
            raw = await client.generate_from_images(prompt, [e["bytes"] for e in batch])
            hits = prompts.parse_scan_response(raw, valid_z={int(e["z"]) for e in batch})
            for h in hits:
                z = int(h["z"])
                finding = h.get("finding", "")
                tagged = f"[{window}] {finding}" if finding else f"[{window}]"
                if z not in flagged or len(tagged) > len(flagged[z]):
                    flagged[z] = tagged
            logger.info(
                "abdomen_ct3_scan_batch",
                window=window, batch=i, of=len(batches), images=len(batch),
                flagged_in_batch=len(hits), flagged_total=len(flagged),
            )

    anomaly_z = sorted(flagged, reverse=True)  # superior→inferior
    flagged_list = [{"z": z, "finding": flagged[z]} for z in anomaly_z]
    any_flagged = bool(anomaly_z)

    # ── Synthesis: compose the report from the flags (TEXT ONLY, no images) ────
    # Cap the flags passed to the synthesis prompt (bounds prompt size); keep an
    # evenly-spread subset so the whole flagged extent is represented.
    if len(flagged_list) > max_report_levels:
        keep = set(_even_pick(anomaly_z, max_report_levels))
        synth_flags = [f for f in flagged_list if f["z"] in keep]
    else:
        synth_flags = flagged_list

    scanned_range = (min(all_z), max(all_z)) if all_z else None
    prompt = prompts.build_synthesis_prompt(
        synth_flags, study_description=study_description, scanned_range=scanned_range
    )
    raw = await client.generate_text(prompt)
    ai_report = prompts.parse_report(raw) if raw else None

    # report_z = the flagged levels (capped) — the hook surfaces these as artifacts.
    report_z = [f["z"] for f in synth_flags]

    logger.info(
        "abdomen_ct3_report_complete",
        scan_windows=scan_windows, scanned=len(all_z), batches=total_batches,
        flagged=len(anomaly_z), synthesized_from=len(synth_flags),
        has_report=bool(ai_report),
    )
    return {
        "anomaly_z": anomaly_z,
        "flagged": flagged_list,
        "report_z": report_z,
        "any_flagged": any_flagged,
        "ai_report": ai_report,
        "batches": total_batches,
    }
