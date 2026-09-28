"""Viewer-session authentication and per-study DICOMweb authorization.

Orthanc is a single PACS shared by every tenant and has no notion of tenancy, so the
platform decides which images a browser may fetch:

* After login the API sets an httpOnly viewer-session cookie (``settings.viewer_cookie_name``)
  holding a ``purpose=viewer`` JWT bound to the user's tenant. OHIF and the browser
  WebSocket send it automatically (same origin); neither can attach our bearer token.
* nginx guards ``/dicom-web/*`` and ``/wado`` with ``auth_request`` → ``GET
  /api/internal/dicomweb-authz``, which resolves the viewer and allows the request only
  when the StudyInstanceUID it names belongs to the viewer's tenant. Study *searches*
  (no UID) are routed to the backend's filtered QIDO proxy instead (dicomweb.py).
* Anything else under DICOMweb (instance/series-level searches with no study, STOW,
  DELETE) is platform-admin only.

Decisions are cached briefly in-process: OHIF issues one request per frame, and each
passes through this hook.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import structlog
from fastapi import APIRouter, Request, Response
from starlette.requests import HTTPConnection
from sqlalchemy import select

from app.application.auth_service import AuthService
from app.config import get_settings
from app.infrastructure.database.models import StudyRecord
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.tenant.db_scope import bind_tenant_scope, reset_scope

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

_STUDY_SEARCH = re.compile(r"^/dicom-web/studies/?$")
_STUDY_SCOPED = re.compile(r"^/dicom-web/studies/([^/?]+)")
_WADO_URI = re.compile(r"^/wado/?$")

_OWNED_TTL = 60.0
_NOT_OWNED_TTL = 10.0
_CACHE_MAX = 20_000
_ownership_cache: dict[tuple[str, str, str], tuple[bool, float]] = {}


@dataclass(frozen=True)
class ViewerIdentity:
    user_id: str
    tenant_id: str
    is_platform_admin: bool
    # Set for the referring Doctor: only studies referred by this user are viewable.
    referring_user_id: str | None = None


def resolve_viewer(request: HTTPConnection) -> ViewerIdentity | None:
    """The viewer-session cookie, or (for API clients) a regular bearer access token.
    Accepts any HTTP connection, so it serves both HTTP requests and WebSocket handshakes."""
    auth = AuthService(session=None)
    token = request.cookies.get(get_settings().viewer_cookie_name)
    if token:
        payload = auth.decode_token(token)
        if payload and payload.get("purpose") == "viewer":
            return _identity(payload)
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        payload = auth.decode_token(header[7:].strip())
        if payload and not payload.get("purpose"):
            return _identity(payload)
    return None


def _identity(payload: dict) -> ViewerIdentity | None:
    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        return None
    return ViewerIdentity(
        user_id=payload.get("sub", ""),
        tenant_id=tenant_id,
        is_platform_admin=bool(payload.get("is_platform_admin", False)),
        referring_user_id=payload.get("ref") or None,
    )


async def owned_study_uids(viewer: ViewerIdentity, study_uids: list[str]) -> set[str]:
    """Which of ``study_uids`` belong to the viewer's tenant (all, for a platform admin)."""
    if viewer.is_platform_admin:
        return set(study_uids)
    if not study_uids:
        return set()
    token = bind_tenant_scope(viewer.tenant_id, referring_user_id=viewer.referring_user_id)
    try:
        async with async_session_factory() as session:
            stmt = select(StudyRecord.study_instance_uid).where(
                StudyRecord.study_instance_uid.in_(study_uids),
                StudyRecord.tenant_id == viewer.tenant_id,
            )
            if viewer.referring_user_id:
                stmt = stmt.where(StudyRecord.referring_user_id == viewer.referring_user_id)
            rows = await session.execute(stmt)
            return {r[0] for r in rows.all()}
    finally:
        reset_scope(token)


async def viewer_can_access_study(viewer: ViewerIdentity, study_uid: str) -> bool:
    if viewer.is_platform_admin:
        return True
    key = (viewer.tenant_id, viewer.referring_user_id or "", study_uid)
    now = time.monotonic()
    cached = _ownership_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    allowed = study_uid in await owned_study_uids(viewer, [study_uid])
    if len(_ownership_cache) >= _CACHE_MAX:
        _ownership_cache.clear()
    _ownership_cache[key] = (allowed, now + (_OWNED_TTL if allowed else _NOT_OWNED_TTL))
    return allowed


def study_uid_from_dicomweb_uri(uri: str) -> tuple[str, str | None]:
    """Classify a proxied DICOMweb URI → (kind, study_uid).

    kind is "search" (study-level QIDO search), "study" (anything scoped to one study),
    or "other" (not study-scoped — platform admins only).
    """
    parts = urlsplit(uri)
    path = parts.path
    # The check runs on the raw request URI while Orthanc may normalise it: a path like
    # /studies/<own-uid>/../<other-uid> must never be classified by its first segment.
    lowered = path.lower()
    if ".." in path or "//" in path or "%2e" in lowered or "%2f" in lowered or "\\" in path:
        return "other", None
    if _STUDY_SEARCH.match(path):
        return "search", None
    m = _STUDY_SCOPED.match(path)
    if m:
        return "study", m.group(1)
    if _WADO_URI.match(path):
        uid = (parse_qs(parts.query).get("studyUID") or [None])[0]
        return ("study", uid) if uid else ("other", None)
    return "other", None


@router.get("/dicomweb-authz")
async def dicomweb_authz(request: Request) -> Response:
    """nginx auth_request target: 204 = allow, 401 = no viewer session, 403 = denied."""
    viewer = resolve_viewer(request)
    if viewer is None:
        return Response(status_code=401)

    method = request.headers.get("X-Original-Method", "GET").upper()
    if method not in ("GET", "HEAD") and not viewer.is_platform_admin:
        return Response(status_code=403)

    uri = request.headers.get("X-Original-URI", "")
    kind, study_uid = study_uid_from_dicomweb_uri(uri)
    if kind == "search":
        allowed = True  # served by the tenant-filtered QIDO proxy (dicomweb.py)
    elif kind == "study" and study_uid:
        allowed = await viewer_can_access_study(viewer, study_uid)
    else:
        allowed = viewer.is_platform_admin

    if not allowed:
        logger.warning(
            "dicomweb_access_denied", tenant_id=viewer.tenant_id, user_id=viewer.user_id,
            kind=kind, study_uid=study_uid,
        )
        return Response(status_code=403)
    return Response(status_code=204)
