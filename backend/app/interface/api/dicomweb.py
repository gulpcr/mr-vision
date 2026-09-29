"""DICOMweb passthroughs for the viewer: tenant filtering, series filtering, MRN display.

Both endpoints authenticate the viewer-session cookie (viewer_access.py) — the PACS is
shared by every tenant, so the study search returns only studies owned by the viewer's
tenant, and a study's series list is only served for a study the viewer may access.


OHIF builds its display sets (thumbnails + viewports) from whatever the QIDO
``/studies/{uid}/series`` query returns. Localizer / shim / calibration / field-map
series are valid DICOM but render as noise and shouldn't be presented as images.
nginx routes only that one QIDO endpoint here; every other DICOMweb request (study
search, instance metadata, WADO pixels) still goes straight to Orthanc, so pixel
retrieval is untouched. On any error — or if filtering would remove every series —
we return Orthanc's response verbatim, so the viewer never breaks because of us.

Patient names: unless DISPLAY_PATIENT_NAMES is on, every DICOM JSON answer the viewer
gets (study search, series list, series/study metadata — nginx routes metadata here too)
carries the MRN in PatientName (0010,0010), and a study search cannot filter by name.
Pixels, bulk data and the DICOM files in the PACS are untouched.
"""
from __future__ import annotations

import json
import re

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response

from app.config import get_settings
from app.interface.api.viewer_access import (
    owned_study_uids,
    resolve_viewer,
    viewer_can_access_study,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/dicomweb", tags=["dicomweb"])

# DICOM JSON model tag keys (group+element, no comma).
_SERIES_DESCRIPTION = "0008103E"
_STUDY_INSTANCE_UID = "0020000D"
_PATIENT_NAME = "00100010"
_PATIENT_ID = "00100020"
_OTHER_PATIENT_NAMES = "00101001"
_DICOM_JSON = "application/dicom+json"
# QIDO filters that would let a name search find (and so reveal) a patient.
_NAME_QUERY_KEYS = {"PatientName", _PATIENT_NAME}


def _names_hidden() -> bool:
    return not get_settings().display_patient_names


def _mask_patient_names(node) -> None:
    """In place: PatientName := the dataset's PatientID (MRN), recursively through
    sequences; other patient names are dropped."""
    if isinstance(node, list):
        for item in node:
            _mask_patient_names(item)
        return
    if not isinstance(node, dict):
        return
    if _PATIENT_NAME in node or _PATIENT_ID in node:
        mrn = _tag_value(node, _PATIENT_ID)
        node[_PATIENT_NAME] = (
            {"vr": "PN", "Value": [{"Alphabetic": mrn}]} if mrn else {"vr": "PN"}
        )
        node.pop(_OTHER_PATIENT_NAMES, None)
    for element in node.values():
        if isinstance(element, dict) and isinstance(element.get("Value"), list):
            for item in element["Value"]:
                if isinstance(item, dict):
                    _mask_patient_names(item)


def _query_params(request: Request) -> dict[str, str]:
    params = dict(request.query_params)
    if _names_hidden():
        params = {k: v for k, v in params.items() if k not in _NAME_QUERY_KEYS}
    return params


def _tag_value(item: dict, tag: str) -> str:
    value = (item.get(tag) or {}).get("Value") or []
    return str(value[0]) if value else ""


@router.get("/studies")
async def filtered_study_search(request: Request) -> Response:
    """QIDO study search, restricted to studies owned by the viewer's tenant.

    Orthanc answers the search across the whole shared PACS; entries whose
    StudyInstanceUID is not in the viewer's tenant are dropped before the response
    leaves the platform. (Orthanc applies ``limit`` before this filter, so a page can
    come back short — acceptable for the viewer's study list.)
    """
    viewer = resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")

    settings = get_settings()
    accept = request.headers.get("accept", _DICOM_JSON)
    async with httpx.AsyncClient(
        auth=(settings.orthanc_username, settings.orthanc_password),
        timeout=httpx.Timeout(60.0, connect=15.0),
    ) as client:
        upstream = await client.get(
            f"{settings.dicomweb_url}/studies",
            params=_query_params(request),
            headers={"Accept": accept},
        )
    if upstream.status_code != 200:
        return Response(content=upstream.content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", _DICOM_JSON))
    try:
        studies = json.loads(upstream.content or b"[]")
    except ValueError:
        # Never pass an unparseable (hence unfilterable) cross-tenant answer through.
        raise HTTPException(status_code=502, detail="Unparseable QIDO response from PACS")

    owned = await owned_study_uids(viewer, [_tag_value(s, _STUDY_INSTANCE_UID) for s in studies])
    kept = [s for s in studies if _tag_value(s, _STUDY_INSTANCE_UID) in owned]
    if _names_hidden():
        _mask_patient_names(kept)
    return Response(content=json.dumps(kept).encode(), media_type=_DICOM_JSON)


def _series_description(series: dict) -> str:
    value = (series.get(_SERIES_DESCRIPTION) or {}).get("Value") or []
    return str(value[0]) if value else ""


@router.get("/studies/{study_uid}/series")
async def filtered_series(study_uid: str, request: Request) -> Response:
    """Proxy the QIDO series query to Orthanc, dropping non-diagnostic series."""
    viewer = resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")
    if not await viewer_can_access_study(viewer, study_uid):
        raise HTTPException(status_code=404, detail="Study not found")
    settings = get_settings()
    url = f"{settings.dicomweb_url}/studies/{study_uid}/series"
    accept = request.headers.get("accept", _DICOM_JSON)

    try:
        async with httpx.AsyncClient(
            auth=(settings.orthanc_username, settings.orthanc_password),
            timeout=httpx.Timeout(60.0, connect=15.0),
        ) as client:
            upstream = await client.get(
                url, params=_query_params(request), headers={"Accept": accept}
            )
    except Exception as exc:
        logger.error("dicomweb_series_proxy_failed", study_uid=study_uid, error=str(exc))
        raise

    content = upstream.content
    media_type = upstream.headers.get("content-type", _DICOM_JSON)

    if settings.viewer_hide_nondiagnostic_series and upstream.status_code == 200:
        try:
            series_list = json.loads(content)
            pattern = re.compile(settings.viewer_nondiagnostic_series_pattern)
            kept = [s for s in series_list if not pattern.search(_series_description(s))]
            dropped = len(series_list) - len(kept)
            # Never hide the whole study — if the filter would empty it, show all.
            if dropped and kept:
                logger.info(
                    "dicomweb_series_filtered",
                    study_uid=study_uid,
                    dropped=dropped,
                    kept=len(kept),
                    hidden=[
                        _series_description(s)
                        for s in series_list
                        if pattern.search(_series_description(s))
                    ],
                )
                content = json.dumps(kept).encode()
                media_type = _DICOM_JSON
        except Exception as exc:
            logger.warning("dicomweb_series_filter_failed", study_uid=study_uid, error=str(exc))

    if _names_hidden() and upstream.status_code == 200:
        content, media_type = _masked_json(content, media_type, study_uid)
    return Response(content=content, media_type=media_type, status_code=upstream.status_code)


def _masked_json(content: bytes, media_type: str, study_uid: str) -> tuple[bytes, str]:
    try:
        data = json.loads(content or b"[]")
    except ValueError:
        # Never pass an unmaskable answer through while names are hidden.
        logger.warning("dicomweb_mask_unparseable", study_uid=study_uid)
        raise HTTPException(status_code=502, detail="Unparseable DICOMweb response from PACS")
    _mask_patient_names(data)
    return json.dumps(data).encode(), _DICOM_JSON


async def _proxy_metadata(request: Request, study_uid: str, path: str) -> Response:
    viewer = resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")
    if not await viewer_can_access_study(viewer, study_uid):
        raise HTTPException(status_code=404, detail="Study not found")
    settings = get_settings()
    async with httpx.AsyncClient(
        auth=(settings.orthanc_username, settings.orthanc_password),
        timeout=httpx.Timeout(120.0, connect=15.0),
    ) as client:
        upstream = await client.get(
            f"{settings.dicomweb_url}{path}",
            params=dict(request.query_params),
            headers={"Accept": request.headers.get("accept", _DICOM_JSON)},
        )
    content = upstream.content
    media_type = upstream.headers.get("content-type", _DICOM_JSON)
    if _names_hidden() and upstream.status_code == 200:
        content, media_type = _masked_json(content, media_type, study_uid)
    return Response(content=content, media_type=media_type, status_code=upstream.status_code)


@router.get("/studies/{study_uid}/series/{series_uid}/metadata")
async def series_metadata(study_uid: str, series_uid: str, request: Request) -> Response:
    """WADO-RS series metadata (what OHIF reads PatientName from) with the MRN shown."""
    return await _proxy_metadata(
        request, study_uid, f"/studies/{study_uid}/series/{series_uid}/metadata"
    )


@router.get("/studies/{study_uid}/metadata")
async def study_metadata(study_uid: str, request: Request) -> Response:
    """WADO-RS study metadata with the MRN shown."""
    return await _proxy_metadata(request, study_uid, f"/studies/{study_uid}/metadata")
