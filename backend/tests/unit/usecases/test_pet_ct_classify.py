"""Unit tests for pet_ct series selection (_classify_series).

Regression coverage for the topogram/scout bug: a PET/CT study carries a
single-slice CT scout (topogram/localizer) alongside the volumetric whole-body
CT. Selecting the scout makes the CT resample onto the PET grid come out all-air
(-1000 HU) — a black CT pane in the viewer. The classifier must pick the
voxel-rich diagnostic CT and skip the scout.
"""
from __future__ import annotations

import pytest

# The pet_ct pipeline imports SimpleITK/torch at module load; skip cleanly when
# those heavy deps are absent (they are present in CI / the worker image).
pytest.importorskip("SimpleITK")

from app.domain.models import Series  # noqa: E402
from app.usecases.pet_ct.pipeline import Pipeline  # noqa: E402


def _series(uid, desc, modality, n, num=2):
    return Series(
        series_instance_uid=uid,
        study_instance_uid="study",
        series_number=n,
        series_description=desc,
        modality=modality,
        num_instances=num,
    )


def _classify(series):
    # _classify_series uses no instance state; skip the heavy __init__.
    return Pipeline._classify_series(Pipeline.__new__(Pipeline), series)


class TestClassifySeries:
    def test_picks_wb_ct_over_topogram(self):
        series = [
            _series("topo", "Topogram  1.0  Tr60", "CT", 1, num=1),
            _series("pet", "PET WB", "PT", 3, num=326),
            _series("ctwb", "CT WB  3.0  Br38", "CT", 4, num=326),
        ]
        out = _classify(series)
        assert out["CT"].series_instance_uid == "ctwb"
        assert out["PET"].series_instance_uid == "pet"

    def test_skips_named_scouts(self):
        for scout_desc in ("Scout", "Localizer", "Surview", "Scanogram AP"):
            series = [
                _series("scout", scout_desc, "CT", 1, num=80),  # named scout, many slices
                _series("ct", "CT WB", "CT", 2, num=300),
                _series("pet", "PET", "PT", 3, num=300),
            ]
            out = _classify(series)
            assert out["CT"].series_instance_uid == "ct", scout_desc

    def test_single_slice_ct_treated_as_scout(self):
        series = [
            _series("thin", "CT recon", "CT", 1, num=2),  # degenerate 2-slice volume
            _series("ct", "CT WB", "CT", 2, num=300),
            _series("pet", "PET", "PT", 3, num=300),
        ]
        out = _classify(series)
        assert out["CT"].series_instance_uid == "ct"

    def test_falls_back_to_largest_when_only_scouts(self):
        # No diagnostic CT — still attempt fusion with the largest available.
        series = [
            _series("topo1", "Topogram", "CT", 1, num=1),
            _series("topo2", "Topogram lateral", "CT", 2, num=1),
            _series("pet", "PET", "PT", 3, num=300),
        ]
        out = _classify(series)
        assert out["CT"].series_instance_uid in ("topo1", "topo2")

    def test_no_ct_series(self):
        series = [_series("pet", "PET", "PT", 1, num=300)]
        out = _classify(series)
        assert "CT" not in out
        assert out["PET"].series_instance_uid == "pet"

    def test_unknown_instance_counts_uses_name_only(self):
        # Runtime QIDO may not populate NumberOfSeriesRelatedInstances → num=0.
        # The named topogram must still be skipped; an unknown count must NOT be
        # treated as a scout (else every CT is excluded and the topogram wins).
        series = [
            _series("topo", "Topogram", "CT", 1, num=0),
            _series("ctwb", "CT WB  3.0  Br38", "CT", 4, num=0),
            _series("pet", "PET WB", "PT", 3, num=0),
        ]
        out = _classify(series)
        assert out["CT"].series_instance_uid == "ctwb"

    def test_picks_largest_pet(self):
        series = [
            _series("pet_thumb", "PET MIP", "PT", 1, num=1),
            _series("pet", "PET WB", "PT", 2, num=326),
        ]
        out = _classify(series)
        assert out["PET"].series_instance_uid == "pet"
