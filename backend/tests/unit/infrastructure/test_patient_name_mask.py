"""MRN instead of patient name in DICOM files served to the viewer (WADO-RS / WADO-URI)."""
from __future__ import annotations

import io

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from app.infrastructure.dicomweb.patient_name_mask import MaskError, mask_multipart, mask_part10


def _instance(name: str = "Doe^Jane", mrn: str = "MRN-42") -> bytes:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientName, ds.PatientID = name, mrn
    ds.OtherPatientNames = "Roe^Jane"
    ref = Dataset()
    ref.PatientName, ref.PatientID = name, mrn
    ds.ReferencedPatientSequence = Sequence([ref])
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.Modality = generate_uid(), generate_uid(), "MR"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def _read(data: bytes) -> Dataset:
    return pydicom.dcmread(io.BytesIO(data))


def test_part10_name_replaced_everything_else_kept():
    original = _instance()
    masked = _read(mask_part10(original))
    src = _read(original)
    assert str(masked.PatientName) == "MRN-42"
    assert "OtherPatientNames" not in masked
    assert str(masked.ReferencedPatientSequence[0].PatientName) == "MRN-42"
    assert masked.PixelData == src.PixelData
    assert masked.SOPInstanceUID == src.SOPInstanceUID
    assert masked.file_meta.TransferSyntaxUID == src.file_meta.TransferSyntaxUID
    assert b"Doe^Jane" not in mask_part10(original)


def _multipart(parts: list[bytes], boundary: str = "abc123") -> tuple[bytes, str]:
    body = b""
    for p in parts:
        body += (f"--{boundary}\r\nContent-Type: application/dicom\r\n"
                 f"Content-Length: {len(p)}\r\n\r\n").encode() + p + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f'multipart/related; type="application/dicom"; boundary={boundary}'


def test_multipart_every_part_masked_and_lengths_recomputed():
    parts = [_instance("Doe^Jane", "MRN-1"), _instance("Roe^Rick", "MRN-2")]
    body, ctype = _multipart(parts)
    out = mask_multipart(body, ctype)
    assert b"Doe^Jane" not in out and b"Roe^Rick" not in out
    chunks = out.split(b"--abc123")
    assert chunks[-1].startswith(b"--")
    names = []
    for chunk in chunks[1:-1]:
        head, _, payload = chunk.partition(b"\r\n\r\n")
        payload = payload[:-2]  # trailing CRLF before the next delimiter
        length = int([h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0]
                     .split(b":")[1])
        assert length == len(payload)
        names.append(str(_read(payload).PatientName))
    assert names == ["MRN-1", "MRN-2"]


@pytest.mark.parametrize("body,ctype", [
    (b"garbage", "multipart/related; boundary=x"),
    (b"--x\r\nContent-Type: application/dicom\r\n\r\nnot dicom\r\n--x--", "multipart/related"),
])
def test_unparseable_answers_raise_instead_of_leaking(body, ctype):
    with pytest.raises(MaskError):
        mask_multipart(body, ctype)
