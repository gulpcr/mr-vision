"""Standalone SAM-Med3D measurement test on the ABIDA CT (run in the worker container)."""
import asyncio
import os
import sys

sys.path.insert(0, "/app")

from app.infrastructure.orthanc.client import OrthancPACSClient  # noqa: E402
from app.usecases.abdomen_ct2.measurement import mass_localization  # noqa: E402
from app.usecases.abdomen_ct2.sammed3d import segment_and_measure  # noqa: E402

STUDY = "1.2.392.200036.9116.2.6.1.37.2420784473.1781919373.826037"
SERIES = "1.2.392.200036.9116.2.6.1.37.2420784473.1781919698.487432"
VOL = "/tmp/abida_ct.nii.gz"
WD = "/tmp/abida_measure"
os.makedirs(WD, exist_ok=True)

if not os.path.exists(VOL):
    async def _dl():
        await OrthancPACSClient().download_series_as_nifti(STUDY, SERIES, VOL)
    asyncio.run(_dl())

flagged = [
    {"z": 244, "finding": "right adnexal mass"},
    {"z": 240, "finding": "right adnexal mass"},
    {"z": 235, "finding": "right adnexal mass"},
    {"z": 231, "finding": "right adnexal mass"},
    {"z": 82, "finding": "right adnexal mass"},
    {"z": 253, "finding": "free fluid"},
]

loc = mass_localization(VOL, flagged, WD, {})
print("LOCALIZATION:", {k: loc.get(k) for k in ("seed_xyz",)} if loc else None)
if loc:
    cfg = {
        "mask_thresh": float(os.environ.get("THR", "0.45")),
        "n_clicks": int(os.environ.get("CLICKS", "6")),
        "target_spacing": float(os.environ.get("TS", "1.0")),
    }
    res = segment_and_measure(VOL, loc, WD, cfg)
    r = res or {}
    print(f"cfg={cfg} -> {r.get('ts_mm')} x {r.get('ap_mm')} x {r.get('cc_mm')} mm | "
          f"{r.get('volume_ml')} ml | clicks={r.get('n_clicks')}")
print("Reference (radiologist): 42 x 55 x 52 mm (TS x AP x CC)")
