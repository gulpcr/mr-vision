"""Unit tests for spine_mri vertebral-level identification.

These cover the per-vertebra level parsing/ordering that replaced the
disc-count heuristic for `levels_analyzed`. The TotalSegmentator runtime path
itself (which requires GPU/weights) is not exercised here.
"""
from __future__ import annotations

import pytest

# The spine pipeline imports SimpleITK/torch at module load; skip cleanly when
# those heavy deps are absent (they are present in CI / the worker image).
pytest.importorskip("SimpleITK")

from app.usecases.spine_mri.pipeline import Pipeline  # noqa: E402


class TestParseVertebraLevel:
    def test_lumbar(self):
        assert Pipeline._parse_vertebra_level("vertebrae_L4.nii.gz") == "L4"

    def test_cervical(self):
        assert Pipeline._parse_vertebra_level("vertebrae_C5.nii.gz") == "C5"

    def test_sacral(self):
        assert Pipeline._parse_vertebra_level("vertebrae_S1.nii.gz") == "S1"

    def test_lowercase_token(self):
        assert Pipeline._parse_vertebra_level("vertebrae_t12.nii.gz") == "T12"

    def test_non_vertebra_returns_none(self):
        assert Pipeline._parse_vertebra_level("spinal_cord.nii.gz") is None

    def test_unknown_level_returns_none(self):
        assert Pipeline._parse_vertebra_level("vertebrae_L9.nii.gz") is None


class TestOrderLevels:
    def test_superior_to_inferior(self):
        assert Pipeline._order_levels({"L5", "C2", "T1", "L1"}) == ["C2", "T1", "L1", "L5"]

    def test_empty(self):
        assert Pipeline._order_levels(set()) == []

    def test_contiguous_lumbar(self):
        assert Pipeline._order_levels({"L1", "L2", "L3", "L4", "L5"}) == [
            "L1", "L2", "L3", "L4", "L5",
        ]


class TestOrderSpinenetLevels:
    """SpineNet labels can include S2 and arrive unordered/duplicated."""

    def test_orders_and_dedups(self):
        assert Pipeline._order_spinenet_levels(["L4", "L5", "L4", "S1"]) == ["L4", "L5", "S1"]

    def test_handles_s2_after_s1(self):
        assert Pipeline._order_spinenet_levels(["S2", "L5", "S1"]) == ["L5", "S1", "S2"]

    def test_unknown_label_sorted_last(self):
        # Unrecognised tokens rank last rather than crashing.
        assert Pipeline._order_spinenet_levels(["ZZ", "L1"]) == ["L1", "ZZ"]

    def test_empty(self):
        assert Pipeline._order_spinenet_levels([]) == []
