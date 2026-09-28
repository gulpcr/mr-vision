"""Review-queue priority of a study: critical › abnormal › normal (pure, stdlib only).

The AI use cases express "how worrying is this study" in different ways (critical-finding
alerts, anomaly slices, BI-RADS, Deauville, CAD-RADS, lesion volume …). This module folds
them into one priority plus the human-readable reasons behind it, so the review queue can
put the studies that need a radiologist first in front of them first.

Rules (the highest matching level wins):

critical
  * any CRITICAL critical-finding alert for the study
  * BI-RADS 5 or 6 · Deauville 5 · CAD-RADS 4B or 5 · tumour / lesion volume ≥ 100 ml
abnormal
  * any WARNING critical-finding alert (quality-only alerts excluded)
  * AI-flagged anomalies (anomaly slices / findings) · detected lesions or tumour
  * BI-RADS 0, 3 or 4 · Deauville 4 · CAD-RADS 3–4A · amyloid positive
normal
  * none of the above

A radiologist may override the computed priority (stored on the study); the override
wins but the computed priority and its reasons are still reported.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

CRITICAL = "critical"
ABNORMAL = "abnormal"
NORMAL = "normal"
PRIORITIES = (CRITICAL, ABNORMAL, NORMAL)
RANK = {CRITICAL: 0, ABNORMAL: 1, NORMAL: 2}

# Alert finding types that describe image/processing quality, not a clinical finding.
_QUALITY_FINDINGS = {"qa_errors"}


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+(\.\d+)?", str(value))
        return float(match.group()) if match else None


def _cad_rads(value: Any) -> tuple[int, str] | None:
    """(grade, suffix) from values like 3, "3", "CAD-RADS 4B", "4A/N"."""
    if value is None:
        return None
    match = re.search(r"([0-5])\s*([AB])?", str(value).upper())
    if not match:
        return None
    return int(match.group(1)), match.group(2) or ""


def _levels_from_summary(usecase: str, summary: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    label = usecase.replace("_", " ")

    for side in ("right", "left"):
        birads = _num(summary.get(f"birads_{side}"))
        if birads is None:
            continue
        birads = int(birads)
        if birads >= 5:
            found.append((CRITICAL, f"BI-RADS {birads} ({side} breast)"))
        elif birads in (0, 3, 4):
            found.append((ABNORMAL, f"BI-RADS {birads} ({side} breast)"))

    deauville = _num(summary.get("deauville_score"))
    if deauville is not None:
        if deauville >= 5:
            found.append((CRITICAL, f"Deauville {int(deauville)}"))
        elif deauville >= 4:
            found.append((ABNORMAL, f"Deauville {int(deauville)}"))

    cad = _cad_rads(summary.get("cad_rads"))
    if cad is not None:
        grade, suffix = cad
        text = f"CAD-RADS {grade}{suffix}"
        if grade == 5 or (grade == 4 and suffix == "B"):
            found.append((CRITICAL, text))
        elif grade >= 3:
            found.append((ABNORMAL, text))

    for key in ("whole_tumor_volume_ml", "total_lesion_volume_ml", "mtv_total_ml"):
        volume = _num(summary.get(key))
        if volume is not None and volume > 0:
            level = CRITICAL if volume >= 100 else ABNORMAL
            found.append((level, f"{label}: lesion volume {volume:.0f} ml"))
            break

    if summary.get("tumor_detected") is True or (_num(summary.get("lesions_detected")) or 0) > 0:
        count = summary.get("lesion_count")
        found.append((ABNORMAL, f"{label}: lesion(s) detected" + (f" ({count})" if count else "")))

    anomalies = summary.get("anomaly_findings") or summary.get("anomaly_slices") or []
    if isinstance(anomalies, list) and anomalies:
        found.append((ABNORMAL, f"{label}: AI flagged {len(anomalies)} abnormal region(s)"))

    if summary.get("amyloid_positive") is True:
        found.append((ABNORMAL, "Amyloid positive"))
    return found


def compute_priority(
    alerts: Iterable[dict[str, Any]],
    results: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[str, list[str]]:
    """``alerts``: dicts with ``severity``, ``finding_type``, ``title``.
    ``results``: (usecase_name, summary) of the study's latest results.
    Returns (priority, reasons) — reasons ordered most severe first."""
    found: list[tuple[str, str]] = []
    for alert in alerts:
        if alert.get("finding_type") in _QUALITY_FINDINGS:
            continue
        severity = str(alert.get("severity") or "").upper()
        title = alert.get("title") or alert.get("finding_type") or "finding"
        if severity == "CRITICAL":
            found.append((CRITICAL, f"Critical finding: {title}"))
        elif severity == "WARNING":
            found.append((ABNORMAL, f"Flagged finding: {title}"))
    for usecase, summary in results:
        found.extend(_levels_from_summary(usecase, summary or {}))

    if not found:
        return NORMAL, []
    found.sort(key=lambda item: RANK[item[0]])
    reasons: list[str] = []
    for _, reason in found:
        if reason not in reasons:
            reasons.append(reason)
    return found[0][0], reasons
