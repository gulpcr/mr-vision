from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app.application.dicom_upload_service import DicomUploadService
from app.application.tenant_api_key_service import TenantApiKeyService
from app.application.tenant_service import TenantService
from app.config import get_settings
from app.infrastructure.ratelimit.redis_limiter import check_rate_limit
from app.interface.api.dependencies import (
    get_dicom_upload_service,
    get_tenant_api_key_service,
    get_tenant_service,
)

router = APIRouter(prefix="/dicom", tags=["dicom-upload"])

REQUIRED_SCOPE = "dicom:upload"


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


@router.post("/upload")
async def upload_dicom(
    request: Request,
    key_service: Annotated[TenantApiKeyService, Depends(get_tenant_api_key_service)],
    tenant_service: Annotated[TenantService, Depends(get_tenant_service)],
    upload_service: Annotated[DicomUploadService, Depends(get_dicom_upload_service)],
    files: list[UploadFile] = File(...),
):
    """Tenant-scoped DICOM ingress. Authenticated by a per-tenant API key
    (`Authorization: Bearer mrv_...`), NOT by the platform JWT/session — this
    route is excluded from RBACMiddleware/TenantResolutionMiddleware entirely
    and does its own auth + tenant resolution, the same way external systems
    push data in without ever holding a user session.
    """
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")

    try:
        key = await key_service.validate_key(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    if not key.has_scope(REQUIRED_SCOPE):
        raise HTTPException(
            status_code=403,
            detail=f"This API key does not have the '{REQUIRED_SCOPE}' scope",
        )

    tenant = await tenant_service.get_tenant(key.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if tenant.status == "suspended":
        raise HTTPException(
            status_code=403,
            detail="This workspace has been suspended. Contact your workspace administrator.",
        )
    if tenant.status == "offboarded":
        raise HTTPException(
            status_code=403,
            detail="This workspace has been offboarded and is no longer accessible.",
        )

    settings = get_settings()
    within_limit = await check_rate_limit(
        f"ratelimit:dicom_upload:{key.id}",
        limit=settings.dicom_upload_rate_limit_per_minute,
        window_seconds=60,
    )
    if not within_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({settings.dicom_upload_rate_limit_per_minute} uploads/min)",
        )

    results = []
    uploaded = 0
    failed = 0
    for f in files:
        dicom_bytes = await f.read()
        try:
            result = await upload_service.upload_instance(
                tenant_id=key.tenant_id, api_key_id=key.id, dicom_bytes=dicom_bytes,
            )
            results.append({"filename": f.filename, "status": "uploaded", **result})
            uploaded += 1
        except ValueError as e:
            results.append({"filename": f.filename, "status": "error", "detail": str(e)})
            failed += 1

    return {
        "tenant_id": key.tenant_id,
        "uploaded": uploaded,
        "failed": failed,
        "results": results,
    }
