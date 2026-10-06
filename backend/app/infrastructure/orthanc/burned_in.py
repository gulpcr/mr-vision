from __future__ import annotations

"""Burned-in text in the pixels (HIPAA Safe Harbor 164.514(b)(2)(i) — identifiers in images).

Ultrasound, secondary captures, screenshots and some angiography frames carry patient name,
MRN or date rendered INTO the image. Header de-identification cannot remove that. Before a
pipeline reads such an image, text is located with OCR (Tesseract) and the boxes are filled
with the image minimum; the event is reported so the result gets a QA flag.

Which images are checked: BurnedInAnnotation (0028,0301) = YES, or a modality / SOP class
that typically carries burned-in text. CT/MR/PT acquisitions are not (their overlays are
separate objects). Fail closed: an image that must be checked but cannot be (no OCR engine,
undecodable pixels) raises BurnedInTextUnverifiable instead of reaching the pipeline.
"""

from dataclasses import dataclass, field

import numpy as np
import structlog
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian

logger = structlog.get_logger(__name__)

TEXT_PRONE_MODALITIES = frozenset({"US", "SC", "OT", "XA", "XC", "ES", "DOC", "SM", "GM", "RF"})
SECONDARY_CAPTURE_PREFIX = "1.2.840.10008.5.1.4.1.1.7"  # SC image storage family
MIN_CONFIDENCE = 55
PAD = 3


class BurnedInTextUnverifiable(RuntimeError):
    """An image that may carry burned-in identifiers could not be checked."""


@dataclass
class RedactionReport:
    checked: int = 0
    redacted_images: int = 0
    boxes: int = 0
    sop_uids: list[str] = field(default_factory=list)

    def qa_flags(self) -> list[str]:
        return ["burned_in_text_redacted"] if self.redacted_images else []


def needs_text_check(ds: Dataset) -> bool:
    if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
        return True
    if str(ds.get("Modality", "")).upper() in TEXT_PRONE_MODALITIES:
        return True
    return str(ds.get("SOPClassUID", "")).startswith(SECONDARY_CAPTURE_PREFIX)


def _to_uint8(frame: np.ndarray) -> np.ndarray:
    f = frame.astype(np.float32)
    lo, hi = float(np.percentile(f, 1)), float(np.percentile(f, 99.5))
    if hi <= lo:
        hi = lo + 1.0
    return (np.clip((f - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def find_text_boxes(frame: np.ndarray, ocr=None) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) of text found in one frame (grayscale or RGB)."""
    if ocr is None:
        try:
            import pytesseract
        except ImportError as exc:
            raise BurnedInTextUnverifiable("OCR engine (pytesseract) not installed") from exc
        ocr = pytesseract
    gray = frame if frame.ndim == 2 else frame[..., :3].mean(axis=-1)
    img = _to_uint8(gray)
    boxes: list[tuple[int, int, int, int]] = []
    # Text is usually light on dark; also try the inverse for dark-on-light captures.
    for candidate in (img, 255 - img):
        try:
            data = ocr.image_to_data(candidate, output_type=ocr.Output.DICT, config="--psm 11")
        except Exception as exc:  # binary missing / crashed
            raise BurnedInTextUnverifiable(f"OCR failed: {exc}") from exc
        for i, text in enumerate(data.get("text", [])):
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if conf >= MIN_CONFIDENCE and len(str(text).strip()) >= 2:
                boxes.append((int(data["left"][i]), int(data["top"][i]),
                              int(data["width"][i]), int(data["height"][i])))
    return boxes


def redact_dataset(ds: Dataset, report: RedactionReport, ocr=None) -> bool:
    """In place: mask burned-in text if the image needs checking. Returns True if masked."""
    if not needs_text_check(ds) or "PixelData" not in ds:
        return False
    report.checked += 1
    try:
        pixels = ds.pixel_array
    except Exception as exc:
        raise BurnedInTextUnverifiable(f"cannot decode pixels: {exc}") from exc
    frames = int(ds.get("NumberOfFrames", 1) or 1)
    rgb = int(ds.get("SamplesPerPixel", 1) or 1) > 1
    stack = pixels if (frames > 1) else pixels[np.newaxis, ...]
    masked = stack.copy()
    total = 0
    for idx in range(stack.shape[0]):
        frame = stack[idx]
        boxes = find_text_boxes(frame, ocr)
        fill = frame.min(axis=(0, 1)) if rgb else frame.min()
        h, w = frame.shape[:2]
        for (x, y, bw, bh) in boxes:
            x0, y0 = max(0, x - PAD), max(0, y - PAD)
            x1, y1 = min(w, x + bw + PAD), min(h, y + bh + PAD)
            masked[idx, y0:y1, x0:x1] = fill
        total += len(boxes)
    if not total:
        return False
    out = masked if frames > 1 else masked[0]
    if ds.file_meta.TransferSyntaxUID.is_compressed:
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.PixelData = out.astype(pixels.dtype).tobytes()
    if rgb:
        ds.PlanarConfiguration = 0
        ds.PhotometricInterpretation = "RGB" if ds.PhotometricInterpretation.startswith("YBR") else ds.PhotometricInterpretation
    ds.BurnedInAnnotation = "NO"
    report.redacted_images += 1
    report.boxes += total
    report.sop_uids.append(str(ds.get("SOPInstanceUID", "")))
    logger.info("burned_in_text_redacted", boxes=total, modality=str(ds.get("Modality", "")))
    return True
