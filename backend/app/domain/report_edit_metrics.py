from __future__ import annotations

"""How much the radiologist changed the AI draft (AI-04 QA & drift). Pure functions.

Computed at signing for every AI result of the study and stored as numbers only (no
report text) in ``ai_report_reviews``:

* word-level Levenshtein distance between the AI draft text and the signed text, and a
  normalised similarity (1 = unchanged);
* for structured reports (mammography): how many AI-proposed slots (BI-RADS, density,
  mass, calcification per breast) the radiologist signed with a different value;
* an outcome class: unchanged / minor_edit / major_edit.
"""

import re
from typing import Any

MAX_TOKENS = 3000
MINOR_EDIT_SIMILARITY = 0.9

# Text fields of an AI result summary that make up its draft report, in a fixed order.
DRAFT_TEXT_KEYS = (
    "ai_report", "consolidated_report", "impression", "findings", "narrative",
    "right_breast_findings", "left_breast_findings", "opinion", "medgemma_findings",
)
# The radiologist-editable text of a mammography report.
MAMMO_TEXT_FIELDS = ("procedure", "clinical_features", "right_breast_findings",
                     "left_breast_findings", "opinion")
MAMMO_SLOTS = ("birads_right", "birads_left", "density_right", "density_left",
               "mass_right", "mass_left", "calcification_right", "calcification_left")


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k in sorted(value) for s in _strings(value[k])]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", (text or "").lower())[:MAX_TOKENS]


def draft_text(summary: dict[str, Any]) -> str:
    return "\n".join(s for k in DRAFT_TEXT_KEYS for s in _strings((summary or {}).get(k)))


def mammo_text(report: dict[str, Any]) -> str:
    return "\n".join(str(report.get(k)) for k in MAMMO_TEXT_FIELDS if report.get(k))


def levenshtein(a: list[str], b: list[str]) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ta in enumerate(a, 1):
        current = [i]
        for j, tb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ta != tb)))
        previous = current
    return previous[-1]


def slot_discrepancies(ai_summary: dict[str, Any], signed: dict[str, Any]) -> tuple[int, int]:
    """(compared, mismatched) structured slots the AI proposed and the report has."""
    compared = mismatched = 0
    for slot in MAMMO_SLOTS:
        ai, final = (ai_summary or {}).get(slot), (signed or {}).get(slot)
        if ai in (None, "") or final in (None, ""):
            continue
        compared += 1
        mismatched += str(ai).strip().lower() != str(final).strip().lower()
    return compared, mismatched


def review_metrics(ai_summary: dict[str, Any], signed_report: dict[str, Any] | None) -> dict[str, Any]:
    """Metrics for one AI result. ``signed_report`` = the radiologist-authored report that
    was signed (mammography); None when the AI text itself was signed unchanged."""
    draft = tokens(draft_text(ai_summary))
    if signed_report is not None and mammo_text(signed_report):
        final = tokens(mammo_text(signed_report))
    else:
        final = draft
    distance = levenshtein(draft, final)
    longest = max(len(draft), len(final))
    similarity = 1.0 if longest == 0 else round(1 - distance / longest, 4)
    compared, mismatched = slot_discrepancies(ai_summary, signed_report or {})
    if distance == 0 and mismatched == 0:
        outcome = "unchanged"
    elif similarity >= MINOR_EDIT_SIMILARITY and mismatched == 0:
        outcome = "minor_edit"
    else:
        outcome = "major_edit"
    return {
        "draft_tokens": len(draft), "final_tokens": len(final), "edit_distance": distance,
        "similarity": similarity, "slots_compared": compared, "slots_mismatched": mismatched,
        "outcome": outcome,
    }
