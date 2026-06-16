"""CPT Code Suggestion Service.

Maps AI findings (use case, body part, measurements) to CPT billing codes.
All suggestions are advisory — radiologist must review and approve.
"""
from __future__ import annotations

from typing import Any

# ── CPT lookup table ──────────────────────────────────────────────────────────
# Structure: {usecase_name: {condition: (code, description, confidence)}}
# Conditions are evaluated against flattened measurement keys and qa_flags.
# A rule without a condition always matches (base code for that use case).

_CPT_RULES: list[dict[str, Any]] = [
    # ── Brain MRI ─────────────────────────────────────────────────────────────
    {
        "usecase": "brain_mri",
        "condition": {"key": "contrast_enhanced", "op": "eq", "value": True},
        "code": "70553",
        "description": "MRI brain w/ and w/o contrast",
        "confidence": 0.95,
        "category": "primary",
    },
    {
        "usecase": "brain_mri",
        "condition": None,  # base code if no contrast info
        "code": "70551",
        "description": "MRI brain w/o contrast",
        "confidence": 0.80,
        "category": "primary",
    },
    {
        "usecase": "brain_mri",
        "condition": {"key": "whole_tumor_volume_ml", "op": "gt", "value": 0.05},
        "code": "70553",
        "description": "MRI brain with contrast (tumor detected)",
        "confidence": 0.92,
        "category": "primary",
    },
    {
        "usecase": "brain_mri",
        "condition": None,
        "code": "0691T",
        "description": "AI-assisted analysis, brain MRI (add-on)",
        "confidence": 0.99,
        "category": "addon",
    },
    # ── Spine MRI ─────────────────────────────────────────────────────────────
    {
        "usecase": "spine_mri",
        "condition": {"key": "region", "op": "eq", "value": "cervical"},
        "code": "72156",
        "description": "MRI spine cervical w/ and w/o contrast",
        "confidence": 0.90,
        "category": "primary",
    },
    {
        "usecase": "spine_mri",
        "condition": {"key": "region", "op": "eq", "value": "thoracic"},
        "code": "72157",
        "description": "MRI spine thoracic w/ and w/o contrast",
        "confidence": 0.90,
        "category": "primary",
    },
    {
        "usecase": "spine_mri",
        "condition": {"key": "region", "op": "eq", "value": "lumbar"},
        "code": "72158",
        "description": "MRI spine lumbar w/ and w/o contrast",
        "confidence": 0.90,
        "category": "primary",
    },
    {
        "usecase": "spine_mri",
        "condition": None,
        "code": "72148",
        "description": "MRI spine lumbar w/o contrast (default)",
        "confidence": 0.75,
        "category": "primary",
    },
    {
        "usecase": "spine_mri",
        "condition": None,
        "code": "0691T",
        "description": "AI-assisted analysis, spine MRI (add-on)",
        "confidence": 0.99,
        "category": "addon",
    },
    # ── Chest MRI ─────────────────────────────────────────────────────────────
    {
        "usecase": "chest_mri",
        "condition": None,
        "code": "71550",
        "description": "MRI thorax w/o contrast",
        "confidence": 0.85,
        "category": "primary",
    },
    {
        "usecase": "chest_mri",
        "condition": {"key": "heart_volume_ml", "op": "gt", "value": 0},
        "code": "75561",
        "description": "Cardiac MRI for function, w/o contrast",
        "confidence": 0.88,
        "category": "primary",
    },
    {
        "usecase": "chest_mri",
        "condition": None,
        "code": "0691T",
        "description": "AI-assisted analysis, chest MRI (add-on)",
        "confidence": 0.99,
        "category": "addon",
    },
    # ── Abdomen MRI ───────────────────────────────────────────────────────────
    {
        "usecase": "abdomen_mri",
        "condition": None,
        "code": "74181",
        "description": "MRI abdomen w/o contrast",
        "confidence": 0.85,
        "category": "primary",
    },
    {
        "usecase": "abdomen_mri",
        "condition": {"key": "liver_volume_ml", "op": "gt", "value": 0},
        "code": "74183",
        "description": "MRI abdomen w/ and w/o contrast",
        "confidence": 0.88,
        "category": "primary",
    },
    {
        "usecase": "abdomen_mri",
        "condition": None,
        "code": "0691T",
        "description": "AI-assisted analysis, abdomen MRI (add-on)",
        "confidence": 0.99,
        "category": "addon",
    },
]


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full_key))
        else:
            out[full_key] = v
    return out


def _evaluate_condition(condition: dict[str, Any] | None, flat: dict[str, Any]) -> bool:
    if condition is None:
        return True
    key = condition["key"]
    op = condition["op"]
    expected = condition["value"]
    actual = flat.get(key)
    if actual is None:
        return False
    if op == "eq":
        return actual == expected
    if op == "gt":
        try:
            return float(actual) > float(expected)
        except (TypeError, ValueError):
            return False
    if op == "gte":
        try:
            return float(actual) >= float(expected)
        except (TypeError, ValueError):
            return False
    if op == "lt":
        try:
            return float(actual) < float(expected)
        except (TypeError, ValueError):
            return False
    if op == "lte":
        try:
            return float(actual) <= float(expected)
        except (TypeError, ValueError):
            return False
    return False


def suggest_cpt_codes(
    usecase_name: str,
    measurements: dict[str, Any],
    summary: dict[str, Any],
    qa_flags: list[str],
) -> list[dict[str, Any]]:
    """Return ranked CPT code suggestions for the given result.

    Returns a list of dicts with: code, description, confidence, category.
    Primary codes are listed first, add-on codes last.
    """
    flat = _flatten(measurements)
    flat.update(_flatten(summary))
    for flag in qa_flags:
        flat[f"qa_flag.{flag}"] = True

    seen_codes: set[str] = set()
    primary: list[dict[str, Any]] = []
    addons: list[dict[str, Any]] = []

    for rule in _CPT_RULES:
        if rule["usecase"] != usecase_name:
            continue
        if not _evaluate_condition(rule["condition"], flat):
            continue
        code = rule["code"]
        if code in seen_codes:
            continue
        seen_codes.add(code)
        entry = {
            "code": code,
            "description": rule["description"],
            "confidence": rule["confidence"],
            "category": rule["category"],
        }
        if rule["category"] == "addon":
            addons.append(entry)
        else:
            primary.append(entry)

    # Sort primary by confidence desc, then append add-ons
    primary.sort(key=lambda x: x["confidence"], reverse=True)
    return primary + addons
