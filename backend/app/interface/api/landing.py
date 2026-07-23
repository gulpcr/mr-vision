from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["landing"])

# backend/app/interface/static/cortex.html — resolved relative to this file so it
# works regardless of the process working directory (container vs. local).
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_CORTEX_HTML = _STATIC_DIR / "cortex.html"


@router.get("/cortex", response_class=HTMLResponse, include_in_schema=False)
async def cortex_landing() -> FileResponse:
    """Serve the Cortex Radiology marketing/landing page as a standalone HTML page.

    Public (whitelisted in RBACMiddleware) — no authentication required.
    """
    if not _CORTEX_HTML.is_file():
        logger.error("cortex_landing_missing", path=str(_CORTEX_HTML))
        return HTMLResponse("<h1>Cortex landing page not found</h1>", status_code=404)
    return FileResponse(_CORTEX_HTML, media_type="text/html")
