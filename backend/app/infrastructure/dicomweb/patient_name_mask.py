from __future__ import annotations

"""Show the MRN instead of the patient's name in DICOM *files* the viewer downloads.

The JSON answers (QIDO, WADO-RS metadata) are masked in interface/api/dicomweb.py. This
module covers the binary retrievals that carry the full header: WADO-RS instance /
series / study retrieve (``multipart/related; type="application/dicom"``) and WADO-URI
(``application/dicom``). Pixel data, transfer syntax and every other element are written
back unchanged; the copy in the PACS is never modified.

Same rule as the JSON masker: PatientName := PatientID (MRN) in the dataset and in any
nested item that carries patient identity; OtherPatientNames is dropped. With
``drop_tags`` (minimum necessary, VIEWER_MINIMUM_NECESSARY) those elements are removed too.
"""

import io
import re

import pydicom
from pydicom.dataset import Dataset
from pydicom.tag import Tag

_PATIENT_NAME = Tag(0x0010, 0x0010)
_PATIENT_ID = Tag(0x0010, 0x0020)
_OTHER_PATIENT_NAMES = Tag(0x0010, 0x1001)
_BOUNDARY = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)


class MaskError(ValueError):
    """The response could not be parsed, so it cannot be passed through masked."""


def _hex_tags(drop_tags) -> list[Tag]:
    return [Tag(int(t, 16)) for t in drop_tags]


def _mask_dataset(ds: Dataset, mask_names: bool = True, drop: list[Tag] | None = None) -> None:
    for tag in drop or ():
        if tag in ds:
            del ds[tag]
    if mask_names and (_PATIENT_NAME in ds or _PATIENT_ID in ds):
        mrn = str(ds.get(_PATIENT_ID).value or "") if _PATIENT_ID in ds else ""
        ds.PatientName = mrn
        if _OTHER_PATIENT_NAMES in ds:
            del ds[_OTHER_PATIENT_NAMES]
    for elem in ds:
        if elem.VR == "SQ" and elem.value:
            for item in elem.value:
                _mask_dataset(item, mask_names, drop)


def mask_part10(data: bytes, mask_names: bool = True, drop_tags=frozenset()) -> bytes:
    """One DICOM Part-10 file with the patient name replaced by the MRN."""
    try:
        ds = pydicom.dcmread(io.BytesIO(data), force=True)
    except Exception as exc:
        raise MaskError(f"unreadable DICOM instance: {exc}") from exc
    _mask_dataset(ds, mask_names, _hex_tags(drop_tags))
    out = io.BytesIO()
    ds.save_as(out, write_like_original=True)
    return out.getvalue()


def mask_multipart(body: bytes, content_type: str, mask_names: bool = True,
                   drop_tags=frozenset()) -> bytes:
    """A ``multipart/related`` WADO-RS answer with every application/dicom part masked.
    Part headers and order are preserved; Content-Length headers are recomputed."""
    m = _BOUNDARY.search(content_type or "")
    if not m:
        raise MaskError("multipart response without a boundary")
    delimiter = b"--" + m.group(1).encode()
    chunks = body.split(delimiter)
    # chunks[0] = preamble, chunks[-1] = "--" + epilogue, the rest = parts.
    if len(chunks) < 3 or not chunks[-1].startswith(b"--"):
        raise MaskError("malformed multipart response")
    out = [chunks[0]]
    for chunk in chunks[1:-1]:
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            raise MaskError("multipart part without headers")
        trailing = b"\r\n" if payload.endswith(b"\r\n") else b""
        content = payload[: len(payload) - len(trailing)]
        headers = [h for h in head.split(b"\r\n") if h]
        ctype = next(
            (h.split(b":", 1)[1].strip().lower() for h in headers
             if h.lower().startswith(b"content-type:")), b"",
        )
        if ctype.startswith(b"application/dicom"):
            content = mask_part10(content, mask_names, drop_tags)
            headers = [h for h in headers if not h.lower().startswith(b"content-length:")]
            headers.append(b"Content-Length: " + str(len(content)).encode())
        out.append(b"\r\n" + b"\r\n".join(headers) + b"\r\n\r\n" + content + trailing)
    out.append(chunks[-1])
    return delimiter.join(out)
