"""Two-pass MedGemma orchestration for ct_brain.

:func:`scan_and_report` runs the whole flow against an already-constructed MedGemma
client (dependency-injected by the Celery task hook — this module imports no
infrastructure and does no file I/O):

  Pass 1 (scan):   the per-slice scan images are chunked into batches of
                   ``batch_size`` and sent to MedGemma one batch at a time, looping
                   until EVERY slice has been reviewed. Each reply flags the abnormal
                   z-levels; they accumulate across batches.
  Pass 2 (report): the flagged levels (capped to ``max_report_levels``, or a
                   representative fallback set when nothing was flagged) are sent back
                   to MedGemma — in every requested HU window — for the structured
                   findings/impression report.

Fully non-blocking by contract: the injected client returns "" on any failure and the
parsers degrade to empty/None, so a MedGemma outage yields an empty result rather than
raising. Returns ``{anomaly_z, flagged, report_z, any_flagged, ai_report, batches}``.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.usecases.ct_brain import prompts

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
    # Integer stride pick without numpy (this module stays dependency-light).
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    seen: list[int] = []
    for i in range(k):
        v = items[round(i * step)]
        if v not in seen:
            seen.append(v)
    return seen


async def rich_read(
    *,
    client: Any,
    images: list[dict[str, Any]],
    flagged: list[dict[str, Any]] | None,
    study_description: str | None,
    demographics: str | None = None,
) -> dict[str, Any] | None:
    """Detailed-read pass: characterize findings + systematic organ review.

    ``images`` is an ordered ``[{"z", "bytes"}]`` set of soft-tissue slices spanning the
    volume (flagged levels + coverage), superior→inferior. Returns
    ``{findings, impression, disclaimer}`` or None on failure. Additive — does not affect
    the scan/flag flow; the caller merges this into ``summary["ai_report"]``.
    """
    if not images:
        return None
    prompt = prompts.build_richread_prompt(
        [int(e["z"]) for e in images], flagged or [], study_description, demographics
    )
    raw = await client.generate_from_images(prompt, [e["bytes"] for e in images])
    parsed = prompts.parse_report(raw) if raw else None
    if parsed:
        # A VLM cannot measure from a slice — strip any numeric size it fabricated.
        parsed["findings"] = prompts.strip_measurements(parsed.get("findings", ""))
        parsed["impression"] = prompts.strip_measurements(parsed.get("impression", ""))
    logger.info("ct_brain_rich_read", images=len(images), has_report=bool(parsed))
    return parsed


async def scan_and_report(
    *,
    client: Any,
    scan_images_by_window: dict[str, list[dict[str, Any]]],
    report_images_by_z: dict[int, list[dict[str, Any]]],
    study_description: str | None,
    windows: list[str],
    batch_size: int = 2,
    max_report_levels: int = 6,
) -> dict[str, Any]:
    """Scan every slice in every scan window, then report on the flagged ones.

    ``scan_images_by_window``: ``{window: [{"z", "name", "bytes"}]}`` — one image per
      candidate slice per SCAN window, superior→inferior. Each window is scanned over
      the whole volume independently and the flagged levels are unioned, so a finding
      visible only in one window (e.g. a bone lesion) is still caught.
    ``report_images_by_z``: ``{z: [{"name", "window", "bytes"}]}`` — the report-window
      images per level (several windows per z), used for the reporting pass.
    """
    scan_windows = [w for w, imgs in scan_images_by_window.items() if imgs]
    if not scan_windows:
        return {"anomaly_z": [], "flagged": [], "report_z": [], "any_flagged": False,
                "ai_report": None, "batches": 0}

    # ── Pass 1: scan the whole volume in each window, unioning the flags ────────
    flagged: dict[int, str] = {}          # z → best finding text (any window)
    total_batches = 0
    for window in scan_windows:
        imgs = scan_images_by_window[window]
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
                # Tag the window so the report hint says where it was seen.
                finding = h.get("finding", "")
                tagged = f"[{window}] {finding}" if finding else f"[{window}]"
                if z not in flagged or len(tagged) > len(flagged[z]):
                    flagged[z] = tagged
            logger.info(
                "ct_brain_scan_batch",
                window=window, batch=i, of=len(batches), images=len(batch),
                flagged_in_batch=len(hits), flagged_total=len(flagged),
            )

    anomaly_z = sorted(flagged, reverse=True)  # superior→inferior
    flagged_list = [{"z": z, "finding": flagged[z]} for z in anomaly_z]

    # Flat scan-image map (first window that has each z) for the Pass-2 fallback.
    scan_bytes_by_z: dict[int, dict[str, Any]] = {}
    for window in scan_windows:
        for e in scan_images_by_window[window]:
            scan_bytes_by_z.setdefault(int(e["z"]), {"name": e["name"], "bytes": e["bytes"]})

    # ── Pass 2: report on the flagged levels (or a fallback sample) ────────────
    any_flagged = bool(anomaly_z)
    if any_flagged:
        report_z = _even_pick(anomaly_z, max_report_levels)
    else:
        # Nothing flagged: still write a report on a representative spread of slices.
        all_z = sorted(scan_bytes_by_z, reverse=True)
        report_z = _even_pick(all_z, min(max_report_levels, 6))

    # Assemble the report images AND a positional layout describing, image-by-image,
    # which (z, window) each one is — so the report prompt can reference images by
    # POSITION (the only thing the model can actually perceive), never by filename.
    report_bytes: list[bytes] = []
    report_layout: list[dict[str, Any]] = []
    for z in report_z:
        imgs = report_images_by_z.get(z) or []
        if not imgs and z in scan_bytes_by_z:  # fall back to a scan-window image
            imgs = [{"name": scan_bytes_by_z[z]["name"], "window": "",
                     "bytes": scan_bytes_by_z[z]["bytes"]}]
        for im in imgs:
            report_bytes.append(im["bytes"])
            report_layout.append({"z": int(z), "window": im.get("window", "")})

    ai_report = None
    if report_layout and report_bytes:
        prompt = prompts.build_report_prompt(
            report_layout=report_layout,
            windows=windows,
            study_description=study_description,
            flagged=flagged_list,
            any_flagged=any_flagged,
        )
        raw = await client.generate_from_images(prompt, report_bytes)
        ai_report = prompts.parse_report(raw) if raw else None

    logger.info(
        "ct_brain_report_complete",
        scan_windows=scan_windows, scanned=len(scan_bytes_by_z), batches=total_batches,
        flagged=len(anomaly_z), reported_levels=len(report_z),
        report_images=len(report_bytes), has_report=bool(ai_report),
    )
    return {
        "anomaly_z": anomaly_z,
        "flagged": flagged_list,
        "report_z": report_z,
        "any_flagged": any_flagged,
        "ai_report": ai_report,
        "batches": total_batches,
    }
