"""PRV-02: text burned into the pixels is masked before inference (fails closed)."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.infrastructure.orthanc.burned_in import (
    BurnedInTextUnverifiable,
    RedactionReport,
    needs_text_check,
    redact_dataset,
)

US_STORAGE = "1.2.840.10008.5.1.4.1.1.6.1"
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"


def _image(modality: str, sop_class: str, pixels: np.ndarray, **tags) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID, ds.Modality = sop_class, meta.MediaStorageSOPInstanceUID, modality
    ds.Rows, ds.Columns = pixels.shape
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = pixels.astype(np.uint8).tobytes()
    for k, v in tags.items():
        setattr(ds, k, v)
    ds.is_little_endian, ds.is_implicit_VR = True, False
    return ds


class _FakeOCR:
    """Reports one text box at (10, 5, 40, 12) on the first (normal) pass."""

    class Output:
        DICT = "dict"

    def __init__(self):
        self.calls = 0

    def image_to_data(self, img, output_type=None, config=""):
        self.calls += 1
        if self.calls == 1:
            return {"text": ["DOE^JANE"], "conf": ["91"], "left": [10], "top": [5], "width": [40], "height": [12]}
        return {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}


def test_which_images_are_checked():
    px = np.zeros((8, 8))
    assert needs_text_check(_image("US", US_STORAGE, px))
    assert needs_text_check(_image("SC", "1.2.840.10008.5.1.4.1.1.7", px))
    assert needs_text_check(_image("CT", CT_STORAGE, px, BurnedInAnnotation="YES"))
    assert not needs_text_check(_image("CT", CT_STORAGE, px))
    assert not needs_text_check(_image("MR", "1.2.840.10008.5.1.4.1.1.4", px))


def test_text_boxes_are_masked_and_reported():
    px = np.full((64, 96), 200, dtype=np.uint8)
    px[0, 0] = 0  # image minimum = fill value
    ds = _image("US", US_STORAGE, px)
    report = RedactionReport()
    assert redact_dataset(ds, report, ocr=_FakeOCR())
    out = np.frombuffer(ds.PixelData, dtype=np.uint8).reshape(64, 96)
    assert (out[5:17, 10:50] == 0).all()           # the box (plus padding) is filled
    assert out[40, 80] == 200                       # the rest of the image is untouched
    assert ds.BurnedInAnnotation == "NO"
    assert report.redacted_images == 1 and report.qa_flags() == ["burned_in_text_redacted"]


def test_ct_acquisitions_are_left_alone():
    ds = _image("CT", CT_STORAGE, np.full((16, 16), 50))
    report = RedactionReport()
    assert not redact_dataset(ds, report, ocr=_FakeOCR())
    assert report.checked == 0


def test_fails_closed_without_an_ocr_engine():
    class Broken(_FakeOCR):
        def image_to_data(self, *a, **k):
            raise OSError("tesseract is not installed")

    with pytest.raises(BurnedInTextUnverifiable):
        redact_dataset(_image("US", US_STORAGE, np.full((16, 16), 9)), RedactionReport(), ocr=Broken())


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
def test_real_ocr_masks_a_rendered_patient_name():
    pytesseract = pytest.importorskip("pytesseract")
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (420, 160), 0)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((12, 10), "DOE JANE  MRN 448812", fill=255, font=font)
    draw.ellipse((150, 70, 270, 150), fill=120)  # "anatomy"
    px = np.array(img)
    ds = _image("US", US_STORAGE, px)
    report = RedactionReport()
    assert redact_dataset(ds, report, ocr=pytesseract)
    out = np.frombuffer(ds.PixelData, dtype=np.uint8).reshape(px.shape)
    assert out[10:45, 12:400].max() < 255           # the text band is masked
    assert out[110, 210] == 120                     # anatomy kept
    text_after = pytesseract.image_to_string(Image.fromarray(out))
    assert "DOE" not in text_after and "448812" not in text_after
