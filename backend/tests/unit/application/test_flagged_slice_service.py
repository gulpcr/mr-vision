"""Unit tests for FlaggedSliceService — tile → DICOM image mapping for CT report results."""
from __future__ import annotations

from typing import Any

from app.application.flagged_slice_service import (
    FlaggedSliceService,
    order_like_volume,
    spacing_is_irregular,
)
from app.domain.models import InstanceGeometry, Result, ResultArtifact

AXIAL = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
SERIES = "1.2.3.series"


def _geom(z_positions: list[float], iop=AXIAL, head_first_numbers: bool = True):
    """Instances numbered 1..N from the head end (highest z), like the test scanner."""
    order = sorted(z_positions, reverse=head_first_numbers)
    return [
        InstanceGeometry(f"sop-{p:g}", order.index(p) + 1, (0.0, 0.0, p), iop)
        for p in z_positions
    ]


class FakeResults:
    def __init__(self, result: Result | None):
        self._r = result

    async def get_result(self, study_uid, usecase, version=None):
        return self._r


class FakePACS:
    def __init__(self, geometry, series_list=None):
        self.geometry = geometry
        self.series_list = series_list or []
        self.geometry_calls: list[str] = []

    async def get_series_instance_geometry(self, study_uid, series_uid):
        self.geometry_calls.append(series_uid)
        return list(self.geometry)

    async def get_series_list(self, study_uid):
        return self.series_list


def _result(summary: dict[str, Any], names: list[str], usecase="abdomen_ct") -> Result:
    return Result(
        study_instance_uid="1.2.3",
        usecase_name=usecase,
        summary=summary,
        artifacts=[
            ResultArtifact(name=n, artifact_type=f"{usecase}_slice_png", storage_path=n)
            for n in names
        ],
    )


def _qido(uid: str, desc: str, count: int) -> dict[str, Any]:
    return {
        "0020000E": {"Value": [uid]},
        "0008103E": {"Value": [desc]},
        "00201209": {"Value": [count]},
    }


# ── ordering ──────────────────────────────────────────────────────────────────


def test_order_is_ascending_along_slice_normal():
    ordered = order_like_volume(_geom([30.0, 10.0, 20.0]))
    assert [g.image_position[2] for _, g in ordered] == [10.0, 20.0, 30.0]


def test_order_follows_reversed_orientation_normal():
    # Row direction flipped → normal points to -z → GDCM stacks highest z first.
    flipped = (-1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    ordered = order_like_volume(_geom([10.0, 30.0, 20.0], iop=flipped))
    assert [g.image_position[2] for _, g in ordered] == [30.0, 20.0, 10.0]


def test_spacing_irregular_detects_gaps():
    assert not spacing_is_irregular([0.0, 1.0, 2.0, 3.0])
    assert spacing_is_irregular([0.0, 1.0, 2.0, 6.0, 7.0])


# ── mapping ───────────────────────────────────────────────────────────────────


async def test_maps_tiles_to_sop_with_window_and_flag():
    positions = [float(p) for p in range(100, 110)]  # 10 slices, z=0 at 100 mm
    summary = {
        "series_instance_uid": SERIES,
        "series_description": "Body 1.0 CE",
        "image_dimensions": [512, 512, 10],
        "window_settings": {"soft-tissue": {"name": "soft-tissue", "level": 40, "width": 400}},
        "anomaly_findings": [{"z": 7, "finding": "[soft-tissue] liver lesion"}],
    }
    names = ["slc_001_z0007_soft-tissue.png", "slc_002_z0002_soft-tissue.png"]
    pacs = FakePACS(_geom(positions))
    svc = FlaggedSliceService(FakeResults(_result(summary, names)), pacs)

    out = await svc.get_slice_links("1.2.3", "abdomen_ct")

    assert out["resolved"] and out["series_instance_uid"] == SERIES
    assert pacs.geometry_calls == [SERIES]
    t7, t2 = out["tiles"]
    assert t7["sop_instance_uid"] == "sop-107"
    assert t7["instance_number"] == 3  # 10 slices numbered from the head: 109→1, 107→3
    assert t7["window"] == {"name": "soft-tissue", "level": 40.0, "width": 400.0}
    assert t7["screen_flagged"] and t7["finding"] == "liver lesion"
    assert t2["screen_flagged"] is False and t2["finding"] is None
    assert [f["sop_instance_uid"] for f in out["flagged_images"]] == ["sop-107"]
    assert out["spacing_irregular"] is False


async def test_missing_images_keep_sop_mapping_and_flag_irregular_spacing():
    positions = [100.0, 101.0, 102.0, 106.0, 107.0]  # gap: images 103-105 missing
    summary = {
        "series_instance_uid": SERIES,
        "image_dimensions": [512, 512, 5],
        "window_settings": {"lung": {"name": "lung", "level": -600, "width": 1500}},
        "anomaly_findings": [{"z": 3, "finding": "nodule"}],
    }
    svc = FlaggedSliceService(
        FakeResults(_result(summary, ["slc_001_z0003_lung.png"], usecase="ct_chest")),
        FakePACS(_geom(positions)),
    )
    out = await svc.get_slice_links("1.2.3", "ct_chest")
    assert out["resolved"] and out["spacing_irregular"]
    assert out["tiles"][0]["sop_instance_uid"] == "sop-106"


async def test_slice_count_mismatch_is_unresolved():
    summary = {"series_instance_uid": SERIES, "image_dimensions": [512, 512, 12]}
    svc = FlaggedSliceService(
        FakeResults(_result(summary, [])), FakePACS(_geom([1.0, 2.0, 3.0]))
    )
    out = await svc.get_slice_links("1.2.3", "abdomen_ct")
    assert out["supported"] and not out["resolved"]
    assert "12" in out["reason"]


# ── series resolution for older results ───────────────────────────────────────


async def test_old_result_resolves_series_by_description_and_count():
    summary = {"series_description": "Body 1.0 CE", "image_dimensions": [512, 512, 3]}
    pacs = FakePACS(
        _geom([1.0, 2.0, 3.0]),
        series_list=[
            _qido("nc", "Body 1.0", 3),
            _qido("ce-short", "Body 1.0 CE", 2),
            _qido("ce", "Body 1.0 CE", 3),
        ],
    )
    svc = FlaggedSliceService(FakeResults(_result(summary, [])), pacs)
    out = await svc.get_slice_links("1.2.3", "abdomen_ct")
    assert out["resolved"] and out["series_instance_uid"] == "ce"


async def test_old_result_ambiguous_series_is_unresolved():
    summary = {"series_description": "Body 1.0 CE", "image_dimensions": [512, 512, 3]}
    pacs = FakePACS(
        _geom([1.0, 2.0, 3.0]),
        series_list=[_qido("a", "Body 1.0 CE", 3), _qido("b", "Body 1.0 CE", 3)],
    )
    svc = FlaggedSliceService(FakeResults(_result(summary, [])), pacs)
    out = await svc.get_slice_links("1.2.3", "abdomen_ct")
    assert not out["resolved"] and "Several" in out["reason"]
    assert pacs.geometry_calls == []


async def test_old_result_windows_fall_back_to_plugin_config():
    summary = {
        "series_instance_uid": SERIES,
        "image_dimensions": [512, 512, 3],
    }
    svc = FlaggedSliceService(
        FakeResults(_result(summary, ["slc_001_z0001_lung.png"], usecase="ct_chest")),
        FakePACS(_geom([1.0, 2.0, 3.0])),
    )
    out = await svc.get_slice_links("1.2.3", "ct_chest")
    assert out["tiles"][0]["window"] == {"name": "lung", "level": -600.0, "width": 1500.0}


# ── tile roles: reported vs overview vs screening-only ────────────────────────


def _ten_slices():
    return _geom([float(p) for p in range(100, 110)])


async def test_roles_from_recorded_summary():
    summary = {
        "series_instance_uid": SERIES,
        "image_dimensions": [512, 512, 10],
        "window_settings": {"st": {"name": "st", "level": 40, "width": 400}},
        "anomaly_findings": [{"z": 8, "finding": "a"}, {"z": 5, "finding": "b"}],
        "preview_z": [9, 5, 1],
        "report_z": [8],
    }
    names = ["slc_001_z0009_st.png", "slc_002_z0005_st.png", "slc_003_z0001_st.png",
             "slc_004_z0008_st.png"]
    svc = FlaggedSliceService(FakeResults(_result(summary, names)), FakePACS(_ten_slices()))
    out = await svc.get_slice_links("1.2.3", "abdomen_ct")
    roles = {t["z"]: (t["reported"], t["overview"], t["screen_flagged"]) for t in out["tiles"]}
    assert roles == {
        9: (False, True, False),   # overview, not flagged
        5: (False, True, True),    # overview that the screening pass flagged
        1: (False, True, False),
        8: (True, False, True),    # reported level
    }
    assert {f["z"]: f["reported"] for f in out["flagged_images"]} == {8: True, 5: False}


async def test_old_result_roles_reconstructed_from_order_and_even_pick():
    # abdomen_ct config: preview_count 6, max_report_levels 10.
    flags = list(range(40, 0, -3))  # 14 Pass-1 flags, superior→inferior
    summary = {
        "series_instance_uid": SERIES,
        "image_dimensions": [512, 512, 50],
        "anomaly_findings": [{"z": z, "finding": "f"} for z in flags],
    }
    from app.application.flagged_slice_service import _even_pick

    reported = _even_pick(flags, 10)
    previews = [49, 39, 29, 19, 9, 0]
    names = [f"slc_{i:03d}_z{z:04d}_soft-tissue.png" for i, z in enumerate(previews)]
    names += [f"slc_{100 + i:03d}_z{z:04d}_soft-tissue.png" for i, z in enumerate(reported)
              if z not in previews]
    svc = FlaggedSliceService(
        FakeResults(_result(summary, names)),
        FakePACS(_geom([float(p) for p in range(50)])),
    )
    out = await svc.get_slice_links("1.2.3", "abdomen_ct")
    got_reported = {t["z"] for t in out["tiles"] if t["reported"]}
    got_overview = {t["z"] for t in out["tiles"] if t["overview"]}
    # A reported level that coincides with a preview reuses the preview tile.
    assert got_reported == set(reported)
    assert got_overview == set(previews) - set(reported)
    assert sum(f["reported"] for f in out["flagged_images"]) == len(reported)


async def test_old_result_roles_fall_back_to_stored_order_when_config_changed():
    # Stored run capped at 2 levels; derivation with today's cap (10) would pick all 4.
    summary = {
        "series_instance_uid": SERIES,
        "image_dimensions": [512, 512, 10],
        "anomaly_findings": [{"z": z, "finding": "f"} for z in (9, 7, 4, 2)],
    }
    previews = [8, 6, 3, 1, 0, 5]
    names = [f"slc_{i:03d}_z{z:04d}_soft-tissue.png" for i, z in enumerate(previews)]
    names += ["slc_101_z0009_soft-tissue.png", "slc_102_z0003_bone.png"]
    svc = FlaggedSliceService(FakeResults(_result(summary, names)), FakePACS(_ten_slices()))
    out = await svc.get_slice_links("1.2.3", "abdomen_ct")
    assert {t["z"] for t in out["tiles"] if t["reported"]} == {9, 3}


# ── scope ─────────────────────────────────────────────────────────────────────


async def test_mri_result_without_geometry_asks_for_a_rerun():
    svc = FlaggedSliceService(
        FakeResults(_result({}, [], usecase="brain_mri")), FakePACS([])
    )
    out = await svc.get_slice_links("1.2.3", "brain_mri")
    assert out["supported"] is True and out["resolved"] is False
    assert "re-run" in out["reason"]


def test_order_uses_normal_of_lowest_instance_number_for_multi_slab_series():
    # Two slabs with different angles in ONE series (disc-angled lumbar axials). The
    # reader stacks along the normal of the lowest-InstanceNumber image, whatever order
    # the PACS lists the instances in.
    tilted = (1.0, 0.0, 0.0, 0.0, 0.8, 0.6)  # normal (0, -0.6, 0.8)
    geo = [
        InstanceGeometry("b2", 4, (0.0, 0.0, 40.0), tilted),
        InstanceGeometry("a1", 1, (0.0, 0.0, 10.0), AXIAL),
        InstanceGeometry("b1", 3, (0.0, 0.0, 30.0), tilted),
        InstanceGeometry("a2", 2, (0.0, 0.0, 20.0), AXIAL),
    ]
    assert [g.sop_instance_uid for _, g in order_like_volume(geo)] == ["a1", "a2", "b1", "b2"]


def _mri_result(panels_by_name: dict[str, list[dict[str, Any]]], flagged_z: list[int]) -> Result:
    geometry = {
        name: {"plane": "sagittal", "reference_sequence": "T2", "panels": panels}
        for name, panels in panels_by_name.items()
    }
    return _result(
        {
            "tile_geometry": geometry,
            "report_z": flagged_z,
            "anomaly_findings": [{"z": z, "finding": "disc bulge"} for z in flagged_z],
        },
        list(panels_by_name),
        usecase="lumbar_spine_mri",
    )


class FakeMultiSeriesPACS:
    def __init__(self, by_series):
        self.by_series = by_series

    async def get_series_instance_geometry(self, study_uid, series_uid):
        if series_uid not in self.by_series:
            raise LookupError(series_uid)
        return list(self.by_series[series_uid])


async def test_mri_panels_resolve_to_their_own_series_or_explain_why_not():
    sag = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)  # sagittal, normal along x
    t2 = [InstanceGeometry(f"t2-{i}", i + 1, (float(i * 4), 0.0, 0.0), sag) for i in range(5)]
    t1 = [InstanceGeometry(f"t1-{i}", 5 - i, (float(i * 4), 0.0, 0.0), sag) for i in range(5)]
    name = "sagittal_003_z00002_montage.png"
    panels = [
        {"sequence": "T2", "series_instance_uid": "T2S", "rect": [0, 0, 0.5, 1],
         "slice_index": 2.0, "slice_spread": 0.0, "n_slices": 5},
        {"sequence": "T1", "series_instance_uid": "T1S", "rect": [0.5, 0, 1, 1],
         "slice_index": 1.9, "slice_spread": 0.05, "n_slices": 5},
        {"sequence": "STIR", "series_instance_uid": "COR", "rect": [0, 0, 1, 1],
         "slice_index": 3.0, "slice_spread": 7.5, "n_slices": 5},
        {"sequence": "PD", "series_instance_uid": "GONE", "rect": [0, 0, 1, 1],
         "slice_index": 1.0, "slice_spread": 0.0, "n_slices": 5},
    ]
    pacs = FakeMultiSeriesPACS({"T2S": t2, "T1S": t1, "COR": t2})
    svc = FlaggedSliceService(FakeResults(_mri_result({name: panels}, [2])), pacs)
    out = await svc.get_slice_links("1.2.3", "lumbar_spine_mri")

    assert out["resolved"] is True
    (tile,) = out["tiles"]
    p = {x["sequence"]: x for x in tile["panels"]}
    assert p["T2"]["clickable"] and p["T2"]["sop_instance_uid"] == "t2-2" and p["T2"]["is_reference"]
    # Nearest slice of its OWN series; stack position follows that series' numbering.
    assert p["T1"]["sop_instance_uid"] == "t1-2" and p["T1"]["stack_position"] == 3
    assert p["T1"]["slice_offset"] == 0.1
    assert not p["STIR"]["clickable"] and "another plane" in p["STIR"]["reason"]
    assert not p["PD"]["clickable"] and "not available" in p["PD"]["reason"]
    # The tile's primary image is the reference panel's.
    assert tile["sop_instance_uid"] == "t2-2" and tile["series_instance_uid"] == "T2S"
    # Flagged level highlighted in every linked series.
    assert {f["sop_instance_uid"] for f in out["flagged_images"]} == {"t2-2", "t1-2"}


async def test_mri_panel_outside_the_series_or_after_it_changed_is_not_linked():
    sag = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
    t2 = [InstanceGeometry(f"t2-{i}", i + 1, (float(i * 4), 0.0, 0.0), sag) for i in range(5)]
    name = "sagittal_001_z00000_montage.png"
    panels = [
        {"sequence": "T2", "series_instance_uid": "T2S", "rect": [0, 0, 0.5, 1],
         "slice_index": 6.0, "slice_spread": 0.0, "n_slices": 5},
        {"sequence": "T1", "series_instance_uid": "T2S", "rect": [0.5, 0, 1, 1],
         "slice_index": 1.0, "slice_spread": 0.0, "n_slices": 9},
    ]
    svc = FlaggedSliceService(
        FakeResults(_mri_result({name: panels}, [])), FakeMultiSeriesPACS({"T2S": t2})
    )
    out = await svc.get_slice_links("1.2.3", "lumbar_spine_mri")
    p = {x["sequence"]: x for x in out["tiles"][0]["panels"]}
    assert "outside" in p["T2"]["reason"] and "re-run" in p["T1"]["reason"]
    assert out["resolved"] is False


async def test_no_result_returns_none():
    svc = FlaggedSliceService(FakeResults(None), FakePACS([]))
    assert await svc.get_slice_links("1.2.3", "abdomen_ct") is None
