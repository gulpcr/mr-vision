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

Minimum necessary (VIEWER_MINIMUM_NECESSARY): study and series searches are cut to the
attributes the viewer shows, and demographics it never shows (birth date, address,
phone, other IDs...) are removed from metadata, instance search and retrieved files.
"""
from __future__ import annotations

import json
import re

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response

from app.config import get_settings
from app.domain.viewer_minimum_necessary import (
    SERIES_QIDO_KEEP,
    STUDY_QIDO_KEEP,
    VIEWER_EXCLUDED_TAGS,
)
from app.infrastructure.tls import httpx_verify
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


def _minimise() -> bool:
    return get_settings().viewer_minimum_necessary


def _rewrites() -> bool:
    """Whether DICOM answers for the viewer are rewritten at all."""
    return _names_hidden() or _minimise()


def _drop_excluded(node) -> None:
    """In place: remove VIEWER_EXCLUDED_TAGS, recursively through sequences."""
    if isinstance(node, list):
        for item in node:
            _drop_excluded(item)
        return
    if not isinstance(node, dict):
        return
    for tag in VIEWER_EXCLUDED_TAGS & node.keys():
        del node[tag]
    for element in node.values():
        if isinstance(element, dict) and isinstance(element.get("Value"), list):
            for item in element["Value"]:
                if isinstance(item, dict):
                    _drop_excluded(item)


def _viewer_view(data, keep: frozenset[str] | None = None):
    """The answer as the viewer may see it: allowlisted (QIDO lists), demographics
    removed, MRN as patient name - each per its setting."""
    if _minimise():
        if keep is not None and isinstance(data, list):
            data = [{k: v for k, v in item.items() if k in keep} if isinstance(item, dict) else item
                    for item in data]
        _drop_excluded(data)
    if _names_hidden():
        _mask_patient_names(data)
    return data


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
    viewer = await resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")

    settings = get_settings()
    accept = request.headers.get("accept", _DICOM_JSON)
    async with httpx.AsyncClient(
        verify=httpx_verify(settings.orthanc_ca_cert),
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
    kept = _viewer_view(
        [s for s in studies if _tag_value(s, _STUDY_INSTANCE_UID) in owned], STUDY_QIDO_KEEP,
    )
    return Response(content=json.dumps(kept).encode(), media_type=_DICOM_JSON)


def _series_description(series: dict) -> str:
    value = (series.get(_SERIES_DESCRIPTION) or {}).get("Value") or []
    return str(value[0]) if value else ""


@router.get("/studies/{study_uid}/series")
async def filtered_series(study_uid: str, request: Request) -> Response:
    """Proxy the QIDO series query to Orthanc, dropping non-diagnostic series."""
    viewer = await resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")
    if not await viewer_can_access_study(viewer, study_uid):
        raise HTTPException(status_code=404, detail="Study not found")
    settings = get_settings()
    url = f"{settings.dicomweb_url}/studies/{study_uid}/series"
    accept = request.headers.get("accept", _DICOM_JSON)

    try:
        async with httpx.AsyncClient(
            verify=httpx_verify(settings.orthanc_ca_cert),
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

    if _rewrites() and upstream.status_code == 200:
        content, media_type = _masked_json(content, media_type, study_uid, SERIES_QIDO_KEEP)
    return Response(content=content, media_type=media_type, status_code=upstream.status_code)


def _masked_json(content: bytes, media_type: str, study_uid: str,
                 keep: frozenset[str] | None = None) -> tuple[bytes, str]:
    try:
        data = json.loads(content or b"[]")
    except ValueError:
        # Never pass an unmaskable answer through while names are hidden.
        logger.warning("dicomweb_mask_unparseable", study_uid=study_uid)
        raise HTTPException(status_code=502, detail="Unparseable DICOMweb response from PACS")
    return json.dumps(_viewer_view(data, keep)).encode(), _DICOM_JSON


async def _proxy_metadata(request: Request, study_uid: str, path: str) -> Response:
    viewer = await resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")
    if not await viewer_can_access_study(viewer, study_uid):
        raise HTTPException(status_code=404, detail="Study not found")
    settings = get_settings()
    async with httpx.AsyncClient(
        verify=httpx_verify(settings.orthanc_ca_cert),
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
    if _rewrites() and upstream.status_code == 200:
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


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def instance_search(study_uid: str, series_uid: str, request: Request) -> Response:
    """QIDO instance search (DICOM JSON) with the MRN shown."""
    return await _proxy_metadata(
        request, study_uid, f"/studies/{study_uid}/series/{series_uid}/instances"
    )


# ── Binary retrieval: whole DICOM files, header included ──────────────────────
# Frames, rendered images, thumbnails and bulk data carry no patient header and still
# go straight to Orthanc; nginx routes only these retrievals here.

async def _proxy_retrieve(request: Request, study_uid: str, url: str, params: dict) -> Response:
    from app.infrastructure.dicomweb.patient_name_mask import (
        MaskError,
        mask_multipart,
        mask_part10,
    )

    viewer = await resolve_viewer(request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Viewer session required")
    if not study_uid or not await viewer_can_access_study(viewer, study_uid):
        raise HTTPException(status_code=404, detail="Study not found")
    settings = get_settings()
    headers = {"Accept": request.headers.get("accept", "*/*")}
    async with httpx.AsyncClient(
        verify=httpx_verify(settings.orthanc_ca_cert),
        auth=(settings.orthanc_username, settings.orthanc_password),
        timeout=httpx.Timeout(300.0, connect=15.0),
    ) as client:
        upstream = await client.get(url, params=params, headers=headers)
    content = upstream.content
    media_type = upstream.headers.get("content-type", "application/octet-stream")
    if _rewrites() and upstream.status_code == 200:
        lowered = media_type.lower()
        opts = {"mask_names": _names_hidden(),
                "drop_tags": VIEWER_EXCLUDED_TAGS if _minimise() else frozenset()}
        try:
            if lowered.startswith("multipart/"):
                content = mask_multipart(content, media_type, **opts)
            elif lowered.startswith("application/dicom") and "json" not in lowered:
                content = mask_part10(content, **opts)
            elif "json" in lowered:
                content, media_type = _masked_json(content, media_type, study_uid)
        except MaskError as exc:
            # Never pass an unmaskable file through while names are hidden.
            logger.warning("dicomweb_retrieve_mask_failed", study_uid=study_uid, error=str(exc))
            raise HTTPException(status_code=502, detail="Unparseable DICOM response from PACS")
    return Response(content=content, media_type=media_type, status_code=upstream.status_code)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
async def retrieve_instance(study_uid: str, series_uid: str, sop_uid: str, request: Request) -> Response:
    """WADO-RS instance retrieve (full DICOM file) with the MRN as patient name."""
    path = f"/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}"
    return await _proxy_retrieve(
        request, study_uid, f"{get_settings().dicomweb_url}{path}", dict(request.query_params)
    )


@router.get("/studies/{study_uid}/series/{series_uid}")
async def retrieve_series(study_uid: str, series_uid: str, request: Request) -> Response:
    """WADO-RS series retrieve (every file of the series) with the MRN as patient name."""
    path = f"/studies/{study_uid}/series/{series_uid}"
    return await _proxy_retrieve(
        request, study_uid, f"{get_settings().dicomweb_url}{path}", dict(request.query_params)
    )


@router.get("/studies/{study_uid}")
async def retrieve_study(study_uid: str, request: Request) -> Response:
    """WADO-RS study retrieve with the MRN as patient name."""
    return await _proxy_retrieve(
        request, study_uid, f"{get_settings().dicomweb_url}/studies/{study_uid}",
        dict(request.query_params),
    )


@router.get("/wado")
async def wado_uri(request: Request) -> Response:
    """WADO-URI (``/wado?requestType=WADO&studyUID=...``). application/dicom answers get
    the MRN as patient name; rendered JPEG/PNG answers pass through untouched."""
    params = dict(request.query_params)
    return await _proxy_retrieve(
        request, params.get("studyUID", ""), f"{get_settings().orthanc_url}/wado", params
    )
