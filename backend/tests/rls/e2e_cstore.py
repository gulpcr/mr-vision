"""Manual end-to-end check for AE-title attribution (not collected by pytest).

Seeds tenant e2e_a owning called AE E2E_HOSPA, C-STOREs one synthetic study to that AE
and one to an unmapped AE, waits for Orthanc's stable-study webhook, then prints how
each study was attributed. Needs: Orthanc (orthanc:4242/8042) with on_stable_study.lua,
the backend reachable as backend:8000, RLS_TEST_OWNER_DATABASE_URL, pynetdicom.
"""
from __future__ import annotations

import os
import sys
import time

import httpx
import numpy as np
import sqlalchemy as sa
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from pynetdicom import AE

OWNER = os.environ["RLS_TEST_OWNER_DATABASE_URL"]


def make_ct(study_uid: str) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientID = "E2E-MRN"
    ds.PatientName = "E2E^Patient"
    ds.Modality = "CT"
    ds.StudyDescription = "E2E TENANT ATTRIBUTION"
    ds.Rows = ds.Columns = 8
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((8, 8), dtype=np.uint16).tobytes()
    return ds


def cstore(ds: Dataset, called: str) -> int:
    ae = AE(ae_title="E2E_SCANNER")
    ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
    assoc = ae.associate("orthanc", 4242, ae_title=called)
    if not assoc.is_established:
        return -1
    status = assoc.send_c_store(ds)
    assoc.release()
    return status.Status


def main() -> int:
    engine = sa.create_engine(OWNER)
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO tenants (id, name, slug, is_active, status, plan, features) VALUES "
            "('e2e_a','E2E A','e2e_a',true,'active','starter','[]') ON CONFLICT (id) DO NOTHING"
        ))
        c.execute(sa.text("DELETE FROM tenant_dicom_endpoints WHERE called_aet = 'E2E_HOSPA'"))
        c.execute(sa.text(
            "INSERT INTO tenant_dicom_endpoints (id, tenant_id, called_aet, is_active) "
            "VALUES ('e2e-ep-1', 'e2e_a', 'E2E_HOSPA', true)"
        ))

    mapped, unmapped = generate_uid(), generate_uid()
    print("cstore mapped  ->", hex(cstore(make_ct(mapped), "E2E_HOSPA")))
    print("cstore unmapped->", hex(cstore(make_ct(unmapped), "NOBODY_AE")))
    print("waiting for stable-study webhook ...")
    time.sleep(int(os.environ.get("E2E_WAIT", "25")))

    with engine.connect() as c:
        for label, uid in (("mapped", mapped), ("unmapped", unmapped)):
            row = c.execute(sa.text(
                "SELECT tenant_id FROM studies WHERE study_instance_uid = :u"), {"u": uid}
            ).first()
            print(f"{label}: study_uid={uid} tenant={row.tenant_id if row else None}")

    orthanc = httpx.Client(base_url="http://orthanc:8042")
    for label, uid in (("mapped", mapped), ("unmapped", unmapped)):
        ids = orthanc.post("/tools/lookup", content=uid).json()
        sid = next((i["ID"] for i in ids if i["Type"] == "Study"), None)
        labels = orthanc.get(f"/studies/{sid}/labels").json() if sid else None
        print(f"{label}: orthanc_labels={labels}")
    print(f"E2E_MAPPED_UID={mapped}")
    print(f"E2E_UNMAPPED_UID={unmapped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
