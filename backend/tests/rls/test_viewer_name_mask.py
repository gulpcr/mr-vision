"""Viewer-fetched DICOM *files* show the MRN, not the patient's name (end to end).

Needs a real Orthanc as well as the RLS database: set RLS_TEST_WITH_ORTHANC=1 plus the
ORTHANC_* settings (the TLS test stack in ops/deploy/README.md does). The test uploads one
instance under the seeded tenant-B study and retrieves it through the backend proxies.
"""
from __future__ import annotations

import io
import os

import pytest

from tests.rls.test_shared_pacs_isolation import _viewer_cookie
from tests.rls.test_tenant_isolation import (  # noqa: F401  (fixtures)
    OWNER_URL,
    STUDY_B,
    TB,
    client,
    seeded,
)

pytestmark = pytest.mark.skipif(
    not (OWNER_URL and os.environ.get("RLS_TEST_WITH_ORTHANC")),
    reason="needs RLS_TEST_OWNER_DATABASE_URL and RLS_TEST_WITH_ORTHANC",
)


@pytest.fixture(scope="module")
def uploaded():
    import httpx
    import numpy as np
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

    from app.config import get_settings
    from app.infrastructure.tls import httpx_verify

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = MRImageStorage, meta.MediaStorageSOPInstanceUID
    ds.PatientName, ds.PatientID = "Secret^Patient", "MRN-MASK-1"
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.Modality = STUDY_B, generate_uid(), "MR"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    s = get_settings()
    r = httpx.post(f"{s.orthanc_url}/instances", content=buf.getvalue(),
                   auth=(s.orthanc_username, s.orthanc_password),
                   verify=httpx_verify(s.orthanc_ca_cert), timeout=30)
    r.raise_for_status()
    yield ds
    httpx.delete(f"{s.orthanc_url}/instances/{r.json()['ID']}",
                 auth=(s.orthanc_username, s.orthanc_password),
                 verify=httpx_verify(s.orthanc_ca_cert), timeout=30)


def _get(client, seeded, path: str, accept: str):
    client.cookies.clear()
    for k, v in _viewer_cookie(seeded["rad_b"], TB).items():
        client.cookies.set(k, v)
    r = client.get(path, headers={"Accept": accept})
    client.cookies.clear()
    return r


def test_instance_retrieve_shows_mrn(client, seeded, uploaded):
    ds = uploaded
    r = _get(client, seeded,
             f"/api/dicomweb/studies/{STUDY_B}/series/{ds.SeriesInstanceUID}/instances/{ds.SOPInstanceUID}",
             'multipart/related; type="application/dicom"; transfer-syntax=*')
    assert r.status_code == 200, r.text[:200]
    assert b"Secret^Patient" not in r.content
    assert b"MRN-MASK-1" in r.content


def test_wado_uri_shows_mrn(client, seeded, uploaded):
    ds = uploaded
    r = _get(client, seeded,
             f"/api/dicomweb/wado?requestType=WADO&studyUID={STUDY_B}"
             f"&seriesUID={ds.SeriesInstanceUID}&objectUID={ds.SOPInstanceUID}"
             "&contentType=application/dicom", "application/dicom")
    assert r.status_code == 200, r.text[:200]
    import pydicom

    assert str(pydicom.dcmread(io.BytesIO(r.content)).PatientName) == "MRN-MASK-1"


def test_other_tenant_cannot_retrieve(client, seeded, uploaded):
    from tests.rls.test_tenant_isolation import TA

    ds = uploaded
    client.cookies.clear()
    for k, v in _viewer_cookie(seeded["rad_a"], TA).items():
        client.cookies.set(k, v)
    r = client.get(f"/api/dicomweb/studies/{STUDY_B}/series/{ds.SeriesInstanceUID}/instances/{ds.SOPInstanceUID}")
    client.cookies.clear()
    assert r.status_code == 404
