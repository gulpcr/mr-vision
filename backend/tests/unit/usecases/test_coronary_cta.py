"""Unit tests for the coronary_cta Agatston calcium scorer and pure helpers.

These cover the deterministic calcium-scoring path against synthetic HU volumes;
the DL stenosis path is stubbed (NotImplementedError) and not exercised here.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.usecases.coronary_cta.pipeline import (
    Pipeline,
    _agatston_weight,
    _cad_rads_from_stenosis,
    _calcium_category,
    _compute_agatston,
)

# Explicit config so tests don't depend on inference_config.yaml values.
CFG = {
    "hu_threshold": 130,
    "min_lesion_area_mm2": 1.0,
    "reference_slice_thickness_mm": 3.0,
    "roi_x_lo": 0.30,
    "roi_x_hi": 0.80,
    "roi_y_lo": 0.25,
    "roi_y_hi": 0.78,
    "roi_z_lo": 0.10,
    "roi_z_hi": 0.90,
}

# 1 mm in-plane, 3 mm slices → pixel area 1 mm², thickness correction 1.0.
SPACING = (1.0, 1.0, 3.0)
SHAPE = (100, 100, 40)


def _empty_volume(fill: float = -50.0) -> np.ndarray:
    """Sub-threshold soft-tissue background (HU well below 130)."""
    return np.full(SHAPE, fill, dtype=np.float32)


def _place_lesion(vol: np.ndarray, x0: int, y0: int, z: int, size: int, hu: float) -> None:
    """Set a size×size block on one axial slice to a given HU."""
    vol[x0 : x0 + size, y0 : y0 + size, z] = hu


# ── _agatston_weight ────────────────────────────────────────────────────────────

class TestAgatstonWeight:
    @pytest.mark.parametrize(
        "hu,expected",
        [(0, 0), (129, 0), (130, 1), (199, 1), (200, 2), (299, 2),
         (300, 3), (399, 3), (400, 4), (1200, 4)],
    )
    def test_density_tiers(self, hu, expected):
        assert _agatston_weight(hu) == expected


# ── _calcium_category ─────────────────────────────────────────────────────────

class TestCalciumCategory:
    @pytest.mark.parametrize(
        "score,expected",
        [(0, "Zero (no detectable calcium)"), (5, "Minimal"), (10, "Minimal"),
         (11, "Mild"), (100, "Mild"), (101, "Moderate"), (400, "Moderate"),
         (401, "Severe"), (2000, "Severe")],
    )
    def test_boundaries(self, score, expected):
        assert _calcium_category(score) == expected


# ── _cad_rads_from_stenosis ─────────────────────────────────────────────────────

class TestCadRads:
    @pytest.mark.parametrize(
        "pct,expected",
        [(0, 0), (1, 1), (24, 1), (25, 2), (49, 2), (50, 3),
         (69, 3), (70, 4), (99, 4), (100, 5)],
    )
    def test_boundaries(self, pct, expected):
        assert _cad_rads_from_stenosis(pct) == expected


# ── _compute_agatston ────────────────────────────────────────────────────────────

class TestComputeAgatston:
    def test_single_lesion_known_score(self):
        vol = _empty_volume()
        # 3×3 block (9 mm²) at HU 250 (weight 2) on one slice, inside ROI.
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=250.0)

        res = _compute_agatston(vol, SPACING, CFG)

        assert res["lesion_count"] == 1
        # area 9 mm² × weight 2 × (3/3 correction) = 18
        assert res["agatston_score"] == pytest.approx(18.0)
        # 9 voxels × (1×1×3) mm³ = 27 mm³
        assert res["calcium_volume_mm3"] == pytest.approx(27.0)
        assert res["calcium_mask"].sum() == 9

    def test_weight_scales_with_peak_hu(self):
        vol = _empty_volume()
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=450.0)  # weight 4
        res = _compute_agatston(vol, SPACING, CFG)
        assert res["agatston_score"] == pytest.approx(9 * 4 * 1.0)  # 36

    def test_slice_thickness_correction(self):
        vol = _empty_volume()
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=250.0)
        # 1.5 mm slices → correction 1.5/3 = 0.5 → score halved.
        res = _compute_agatston(vol, (1.0, 1.0, 1.5), CFG)
        assert res["agatston_score"] == pytest.approx(9 * 2 * 0.5)  # 9

    def test_below_threshold_not_counted(self):
        vol = _empty_volume()
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=120.0)  # < 130
        res = _compute_agatston(vol, SPACING, CFG)
        assert res["lesion_count"] == 0
        assert res["agatston_score"] == 0.0

    def test_calcium_outside_roi_excluded(self):
        vol = _empty_volume()
        # Corner (x=2,y=2) is outside the central cardiac bounding box.
        _place_lesion(vol, x0=2, y0=2, z=20, size=3, hu=400.0)
        res = _compute_agatston(vol, SPACING, CFG)
        assert res["lesion_count"] == 0
        assert res["agatston_score"] == 0.0

    def test_subthreshold_area_excluded(self):
        vol = _empty_volume()
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=300.0)  # 9 mm²
        cfg = {**CFG, "min_lesion_area_mm2": 10.0}  # raise min above lesion area
        res = _compute_agatston(vol, SPACING, cfg)
        assert res["lesion_count"] == 0

    def test_multiple_lesions_summed(self):
        vol = _empty_volume()
        _place_lesion(vol, x0=40, y0=40, z=15, size=3, hu=250.0)  # 9×2 = 18
        _place_lesion(vol, x0=60, y0=60, z=25, size=2, hu=400.0)  # 4×4 = 16
        res = _compute_agatston(vol, SPACING, CFG)
        assert res["lesion_count"] == 2
        assert res["agatston_score"] == pytest.approx(18.0 + 16.0)

    def test_no_calcium_returns_zero(self):
        res = _compute_agatston(_empty_volume(), SPACING, CFG)
        assert res["lesion_count"] == 0
        assert res["agatston_score"] == 0.0
        assert res["calcium_volume_mm3"] == 0.0
        assert res["calcium_mask"].sum() == 0


# ── _compute_agatston with an injected (heart-mask) ROI ─────────────────────────

class TestComputeAgatstonWithExplicitRoi:
    """The TotalSegmentator heart-mask path supplies a precomputed boolean ROI.

    The scoring math must be identical to the heuristic-box path — only the
    spatial restriction changes.
    """

    def test_explicit_roi_includes_lesion(self):
        vol = _empty_volume()
        # Lesion in a corner that the heuristic central box would EXCLUDE...
        _place_lesion(vol, x0=2, y0=2, z=20, size=3, hu=250.0)
        # ...but an explicit ROI covering that corner includes it.
        roi = np.zeros(SHAPE, dtype=bool)
        roi[0:10, 0:10, :] = True

        res = _compute_agatston(vol, SPACING, CFG, roi=roi)
        assert res["lesion_count"] == 1
        assert res["agatston_score"] == pytest.approx(18.0)  # 9 mm² × weight 2

    def test_explicit_roi_excludes_outside(self):
        vol = _empty_volume()
        # Lesion at the centre (inside the heuristic box) but OUTSIDE our ROI.
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=400.0)
        roi = np.zeros(SHAPE, dtype=bool)
        roi[0:10, 0:10, :] = True  # ROI nowhere near the lesion

        res = _compute_agatston(vol, SPACING, CFG, roi=roi)
        assert res["lesion_count"] == 0
        assert res["agatston_score"] == 0.0

    def test_explicit_roi_matches_box_when_equivalent(self):
        """Same score whether the central region is selected by box or by ROI."""
        vol = _empty_volume()
        _place_lesion(vol, x0=49, y0=49, z=20, size=3, hu=300.0)

        box_res = _compute_agatston(vol, SPACING, CFG)
        full_roi = np.ones(SHAPE, dtype=bool)
        roi_res = _compute_agatston(vol, SPACING, CFG, roi=full_roi)
        assert roi_res["agatston_score"] == pytest.approx(box_res["agatston_score"])


# ── Lumen stenosis grader (shared by the SwinUNETR and TotalSegmentator sources) ─

class TestGradeLumenMask:
    """The grader is source-agnostic: it grades whatever binary lumen mask it is
    handed, whether from the learned model or TotalSegmentator's coronary_arteries
    task. Empty / None input must short-circuit to no segments."""

    def test_empty_mask_returns_no_segments(self):
        p = Pipeline()
        out = p._grade_lumen_mask(np.zeros((10, 10, 10), dtype=np.uint8), (1.0, 1.0, 1.0))
        assert out == {"segments": [], "max_stenosis_pct": 0.0}

    def test_none_mask_returns_no_segments(self):
        p = Pipeline()
        out = p._grade_lumen_mask(None, (1.0, 1.0, 1.0))
        assert out == {"segments": [], "max_stenosis_pct": 0.0}

    def test_totalseg_coronary_enabled_by_default(self):
        # Option 1: the no-weights TotalSegmentator coronary lumen source is the
        # configured default fallback when no learned model is present.
        p = Pipeline()
        assert p._model is None
        assert p._cfg.get("coronary_lumen", {}).get("use_totalseg_coronary") is True
