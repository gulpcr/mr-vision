"""DICOMweb QIDO passthroughs for the viewer: tenant filtering + series filtering.

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
_DICOM_JSON = "application/dicom+json"


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
            params=dict(request.query_params),
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
                url, params=dict(request.query_params), headers={"Accept": accept}
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

    return Response(content=content, media_type=media_type, status_code=upstream.status_code)
