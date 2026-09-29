"""Display-time correction of report-family tiles stored before the orientation fix."""
from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

# tests/integration/conftest.py replaces PIL with a MagicMock for the whole pytest process;
# these tests need the real library (run them on their own: pytest tests/unit).
pytestmark = pytest.mark.skipif(
    isinstance(sys.modules.get("PIL"), MagicMock),
    reason="PIL is mocked by tests/integration/conftest.py in this session",
)

from PIL import Image  # noqa: E402

from app.application.tile_orientation import correct_tile, needs_correction  # noqa: E402


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def _arr(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)))


def test_only_old_report_family_tiles_are_corrected():
    old, new = {}, {"tile_orientation": "dicom"}
    assert needs_correction("ct_chest", "slc_001_z00012_lung.png", old)
    assert needs_correction("lumbar_spine_mri", "sagittal_001_z00013_montage.png", old)
    assert not needs_correction("ct_chest", "slc_001_z00012_lung.png", new)
    assert not needs_correction("pet_ct", "slc_001_z00012_lung.png", old)
    assert not needs_correction("ct_chest", "overview.png", old)


def test_ct_tile_is_flipped_back_upright():
    img = np.zeros((4, 6), np.uint8)
    img[0, :] = 200  # stored upside down: anterior content at the bottom → fixed to top
    out = _arr(correct_tile(_png(img[::-1]), "ct_chest"))
    assert (out[0] == 200).all() and (out[-1] == 0).all()


def test_mri_montage_panels_are_mirrored_in_place_labels_untouched():
    tile, label = 448, 28  # plugin defaults: tile_size 448 → label bar 28 px
    mont = np.zeros((tile + label, 2 * tile), np.uint8)
    mont[:label, 4:40] = 255                     # label text of panel 1 (top-left)
    mont[label:, 0:10] = 100                     # panel 1 content hugging its left edge
    mont[label:, tile:tile + 10] = 150           # panel 2 content hugging its left edge
    out = _arr(correct_tile(_png(mont), "lumbar_spine_mri"))
    assert (out[:label, 4:40] == 255).all()                     # label kept, not mirrored
    assert (out[label:, tile - 10:tile] == 100).all()           # panel 1 now on its right edge
    assert (out[label:, 2 * tile - 10:] == 150).all()           # panel 2 stays in panel 2
    assert (out[label:, 0:10] == 0).all()


def test_unknown_montage_layout_is_left_unchanged():
    data = _png(np.full((300, 300), 7, np.uint8))
    assert correct_tile(data, "brain_mri") == data
