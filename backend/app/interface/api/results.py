import tempfile
from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.result_service import ResultService
from app.interface.api.dependencies import get_result_service, get_session
from app.interface.schemas.result import (
    ArtifactResponse,
    CompareRequest,
    CompareResponse,
    DeltaResponse,
    MeasurementDelta,
    ResultListResponse,
    ResultResponse,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["results"])


@router.post("/compare", response_model=CompareResponse)
async def compare_results(
    body: CompareRequest,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """Compare two results by ID and return a delta payload."""
    if len(body.result_ids) != 2:
        raise HTTPException(400, "Exactly 2 result_ids required")
    try:
        data = await service.compare_results(body.result_ids[0], body.result_ids[1])
    except ValueError as e:
        raise HTTPException(404, str(e))

    ra = _to_response(data["result_a"])
    rb = _to_response(data["result_b"])
    d = data["delta"]
    return CompareResponse(
        usecase_name=ra.usecase_name,
        result_a=ra,
        result_b=rb,
        delta=DeltaResponse(
            measurements={
                k: MeasurementDelta(**v) for k, v in d["measurements"].items()
            },
            qa_flags_new=d["qa_flags_new"],
            qa_flags_resolved=d["qa_flags_resolved"],
            days_between=d["days_between"],
        ),
    )


@router.get("/results/{study_uid}/{usecase}", response_model=ResultResponse)
async def get_result(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    version: int | None = Query(default=None, description="Specific result version (omit for latest)"),
):
    result = await service.get_result(study_uid, usecase, version=version)
    if not result:
        detail = f"No results found for study {study_uid} / use case {usecase}"
        if version is not None:
            detail += f" / version {version}"
        raise HTTPException(status_code=404, detail=detail)
    return _to_response(result)


@router.get("/results/{study_uid}/{usecase}/versions", response_model=ResultListResponse)
async def list_result_versions(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """List all result versions for a study/use-case pair, newest first."""
    results = await service.list_result_versions(study_uid, usecase)
    return ResultListResponse(results=[_to_response(r) for r in results])


@router.get("/results/{study_uid}/{usecase}/cpt-suggestions")
async def get_cpt_suggestions(
    study_uid: str,
    usecase: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """Return ranked CPT billing code suggestions for the latest AI result."""
    result = await service.get_result(study_uid, usecase)
    if not result:
        raise HTTPException(404, "No result found")
    from app.application.cpt_service import suggest_cpt_codes

    qa_flags = [f.value if hasattr(f, "value") else f for f in result.qa_flags]
    suggestions = suggest_cpt_codes(
        usecase_name=usecase,
        measurements=result.measurements,
        summary=result.summary,
        qa_flags=qa_flags,
    )
    return {"result_id": result.id, "usecase_name": usecase, "suggestions": suggestions}


@router.get("/results/{result_id}/report.pdf")
async def download_report_pdf(
    result_id: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    session: Annotated["AsyncSession", Depends(get_session)],
):
    """Generate and download a PDF auto-draft report for the given result ID."""
    from fastapi.responses import StreamingResponse
    import io

    result = await service.get_result_by_id(result_id)
    if not result:
        raise HTTPException(404, "Result not found")

    from app.infrastructure.database.models import StudyRecord
    from sqlalchemy import select

    stmt = select(StudyRecord).where(StudyRecord.study_instance_uid == result.study_instance_uid)
    res = await session.execute(stmt)
    study_rec = res.scalar_one_or_none()

    from app.application.report_service import generate_report_pdf

    qa_flags = [f.value if hasattr(f, "value") else f for f in result.qa_flags]
    pdf_bytes = generate_report_pdf(
        study_uid=result.study_instance_uid,
        patient_name=study_rec.patient_name if study_rec else None,
        patient_id=study_rec.patient_id if study_rec else None,
        study_date=study_rec.study_date if study_rec else None,
        study_description=study_rec.study_description if study_rec else None,
        institution=study_rec.institution_name if study_rec else None,
        usecase_name=result.usecase_name,
        model_version=result.model_version,
        measurements=result.measurements,
        summary=result.summary,
        qa_flags=qa_flags,
        result_created_at=result.created_at,
    )

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report_{result_id[:8]}.pdf"'},
    )


@router.post("/results/{result_id}/share")
async def create_share_link(
    result_id: str,
    body: dict,
    service: Annotated[ResultService, Depends(get_result_service)],
    session: Annotated["AsyncSession", Depends(get_session)],
):
    """Create a time-limited share link for referring physician portal access."""
    result = await service.get_result_by_id(result_id)
    if not result:
        raise HTTPException(404, "Result not found")

    from app.application.portal_service import PortalService

    portal = PortalService(session)
    link = await portal.create_share_link(
        result_id=result_id,
        study_instance_uid=result.study_instance_uid,
        usecase_name=result.usecase_name,
        created_by=body.get("created_by", "system"),
        ttl_days=int(body.get("ttl_days", 7)),
    )
    await session.commit()
    return link


@router.get("/portal/{token}")
async def get_portal_result(
    token: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    session: Annotated["AsyncSession", Depends(get_session)],
):
    """Read-only portal endpoint for referring physicians. Validates the share token."""
    from app.application.portal_service import PortalService

    portal = PortalService(session)
    link = await portal.resolve_token(token)
    if not link:
        raise HTTPException(403, "Invalid or expired share link")

    result = await service.get_result_by_id(link["result_id"])
    if not result:
        raise HTTPException(404, "Result not found")

    from app.infrastructure.database.models import StudyRecord
    from sqlalchemy import select

    stmt = select(StudyRecord).where(StudyRecord.study_instance_uid == result.study_instance_uid)
    res = await session.execute(stmt)
    study_rec = res.scalar_one_or_none()

    return {
        "portal": True,
        "expires_at": link["expires_at"],
        "result": _to_response(result),
        "study": {
            "patient_name": study_rec.patient_name if study_rec else None,
            "patient_id": study_rec.patient_id if study_rec else None,
            "study_date": study_rec.study_date.isoformat() if study_rec and study_rec.study_date else None,
            "study_description": study_rec.study_description if study_rec else None,
            "institution_name": study_rec.institution_name if study_rec else None,
        },
    }


@router.get("/results/{study_uid}", response_model=ResultListResponse)
async def list_study_results(
    study_uid: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    results = await service.list_results_for_study(study_uid)
    return ResultListResponse(results=[_to_response(r) for r in results])


@router.get("/preview/{study_uid}/{usecase}/{view}")
async def get_preview(
    study_uid: str,
    usecase: str,
    view: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """Return a preview PNG with segmentation overlay on the MRI slice.

    Checks if a pre-generated preview exists in storage. If not, generates
    one on-demand from the segmentation NIfTI and DICOM data from Orthanc.
    """
    if view not in ("axial", "coronal", "sagittal"):
        raise HTTPException(400, "view must be axial, coronal, or sagittal")

    filename = f"preview_{view}.png"

    # Try to serve pre-generated preview from storage
    try:
        data = await service.get_artifact_data(study_uid, usecase, filename)
        return Response(content=data, media_type="image/png")
    except Exception:
        pass  # Not found — generate on-demand

    logger.info("generating_preview_on_demand", study_uid=study_uid, usecase=usecase, view=view)

    # On-demand generation: download segmentation + DICOM, render overlay
    try:
        result = await service.get_result(study_uid, usecase)
        if not result:
            raise HTTPException(404, "No result found")

        # Per-usecase segmentation artifact storage key (matches artifact["name"] in pipeline)
        SEG_ARTIFACT: dict[str, str] = {
            "pet_ct": "lesion_mask",
            "pet_ct_brain": "lesion_mask",
        }
        seg_filename = SEG_ARTIFACT.get(usecase, "segmentation.nii.gz")
        seg_data = await service.get_artifact_data(study_uid, usecase, seg_filename)

        import nibabel as nib
        import numpy as np

        # Download one DICOM series from Orthanc and convert to NIfTI
        from app.infrastructure.orthanc.client import OrthancPACSClient

        pacs = OrthancPACSClient()
        try:
            series_list = await pacs.get_series_list(study_uid)
            if not series_list:
                raise ValueError("No series found in Orthanc")

            # Preferred background modality per usecase (CT for PET-CT, else MR)
            BG_MODALITY: dict[str, str] = {
                "pet_ct": "CT",
                "pet_ct_brain": "CT",
            }
            preferred_mod = BG_MODALITY.get(usecase, "MR")

            from app.infrastructure.dicomweb.client import DICOMwebClient
            ext = DICOMwebClient.extract_tag_value
            target_series = None
            for s in series_list:
                mod = ext(s, "Modality") or ""
                if mod.upper() == preferred_mod:
                    target_series = ext(s, "SeriesInstanceUID")
                    break
            if not target_series:
                target_series = ext(series_list[0], "SeriesInstanceUID")

            with tempfile.TemporaryDirectory(prefix="preview_") as tmpdir:
                import os

                # Write segmentation to temp file (nibabel needs file for .nii.gz)
                seg_path = os.path.join(tmpdir, "segmentation.nii.gz")
                with open(seg_path, "wb") as f:
                    f.write(seg_data)
                seg_img = nib.load(seg_path)
                seg_array = np.asarray(seg_img.dataobj, dtype=np.uint8)
                seg_affine = seg_img.affine

                # Download DICOM and convert to NIfTI
                nifti_path = os.path.join(tmpdir, "volume.nii.gz")
                await pacs.download_series_as_nifti(study_uid, target_series, nifti_path)

                # Load and resample to segmentation space
                import SimpleITK as sitk

                orig_sitk = sitk.ReadImage(nifti_path)
                seg_sitk = sitk.GetImageFromArray(seg_array.transpose(2, 1, 0))
                seg_sitk.SetOrigin(
                    [float(seg_affine[i, 3]) for i in range(3)]
                )
                seg_sitk.SetSpacing(
                    [float(abs(seg_affine[i, i])) for i in range(3)]
                )

                resampler = sitk.ResampleImageFilter()
                resampler.SetReferenceImage(seg_sitk)
                resampler.SetInterpolator(sitk.sitkLinear)
                resampled = resampler.Execute(orig_sitk)
                bg_array = sitk.GetArrayFromImage(resampled).transpose(2, 1, 0).astype(np.float32)

                from app.services.preview_generator import generate_preview_bytes

                png_bytes = generate_preview_bytes(
                    bg_array, seg_array, view=view, target_size=512
                )

                # Cache it in storage for next time
                try:
                    await service.store_artifact(
                        study_uid, usecase, filename, png_bytes, "image/png"
                    )
                except Exception:
                    pass  # Non-critical

                return Response(content=png_bytes, media_type="image/png")
        finally:
            await pacs.close()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("preview_generation_failed", error=str(e))
        raise HTTPException(500, f"Preview generation failed: {str(e)}")


@router.get("/fused/{study_uid}/{usecase}/{view}")
async def get_fused_image(
    study_uid: str,
    usecase: str,
    view: str,
    service: Annotated[ResultService, Depends(get_result_service)],
):
    """Serve a fused PET/CT PNG.

    Attempts to serve a pre-generated artifact from storage first.
    Falls back to on-demand generation from the stored SUV NIfTI (and CT NIfTI
    if available) so that old results that pre-date fused-image generation still
    display correctly.
    """
    if view not in ("axial", "coronal", "sagittal"):
        raise HTTPException(400, "view must be axial, coronal, or sagittal")

    cached_name = f"fused_{view}.png"

    try:
        data = await service.get_artifact_data(study_uid, usecase, cached_name)
        return Response(content=data, media_type="image/png")
    except Exception:
        pass

    logger.info("generating_fused_on_demand", study_uid=study_uid, usecase=usecase, view=view)

    import os
    import tempfile

    import nibabel as nib
    import numpy as np

    try:
        suv_data = await service.get_artifact_data(study_uid, usecase, "pet_suv")
    except Exception as exc:
        raise HTTPException(404, f"PET SUV artifact not found for {usecase}: {exc}")

    try:
        with tempfile.TemporaryDirectory(prefix="fused_") as tmpdir:
            suv_path = os.path.join(tmpdir, "suv.nii.gz")
            with open(suv_path, "wb") as fh:
                fh.write(suv_data)
            suv_arr = nib.load(suv_path).get_fdata().astype(np.float32)

            ct_arr = None
            try:
                ct_data = await service.get_artifact_data(study_uid, usecase, "ct")
                ct_path = os.path.join(tmpdir, "ct.nii.gz")
                with open(ct_path, "wb") as fh:
                    fh.write(ct_data)
                ct_arr = nib.load(ct_path).get_fdata().astype(np.float32)
            except Exception:
                pass

            from app.services.fused_image_service import generate_fused_png_bytes

            png_bytes = generate_fused_png_bytes(suv_arr, ct_arr, view)

            try:
                await service.store_artifact(study_uid, usecase, cached_name, png_bytes, "image/png")
            except Exception:
                pass

            return Response(content=png_bytes, media_type="image/png")

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("fused_on_demand_failed", study_uid=study_uid, usecase=usecase, error=str(exc))
        raise HTTPException(500, f"Fused image generation failed: {exc}")


@router.get("/artifacts/{study_uid}/{usecase}/{path:path}")
async def get_artifact(
    study_uid: str,
    usecase: str,
    path: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    redirect: bool = True,
):
    try:
        if redirect:
            url = await service.get_artifact_url(study_uid, usecase, path)
            return RedirectResponse(url=url, status_code=307)
        else:
            data = await service.get_artifact_data(study_uid, usecase, path)
            content_type = "application/octet-stream"
            if path.endswith(".json"):
                content_type = "application/json"
            elif path.endswith(".nii.gz"):
                content_type = "application/gzip"
            elif path.endswith(".png"):
                content_type = "image/png"
            return Response(content=data, media_type=content_type)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {str(e)}")


@router.post("/results/{result_id}/export-dicom")
async def export_dicom(
    result_id: str,
    service: Annotated[ResultService, Depends(get_result_service)],
    sr: bool = True,
    seg: bool = False,
):
    """Export a result to Orthanc as DICOM SR and/or DICOM Seg on demand.

    Returns a dict with the Orthanc instance IDs for each exported object.
    Both `sr` and `seg` query params are accepted; at least one must be true.
    """
    from app.config import get_settings
    from app.services.dicom_export_service import DICOMExportService

    if not sr and not seg:
        raise HTTPException(400, "At least one of sr=true or seg=true must be set")

    settings = get_settings()
    if not settings.dicom_sr_enabled and not settings.dicom_seg_enabled:
        raise HTTPException(503, "DICOM export is disabled (set DICOM_SR_ENABLED or DICOM_SEG_ENABLED)")

    result = await service.get_result_by_id(result_id)
    if not result:
        raise HTTPException(404, "Result not found")

    result_data = {
        "id": result.id,
        "study_instance_uid": result.study_instance_uid,
        "usecase_name": result.usecase_name,
        "job_id": result.job_id,
        "summary": result.summary,
        "measurements": result.measurements,
        "qa_flags": [f.value if hasattr(f, "value") else f for f in result.qa_flags],
        "model_version": result.model_version,
        "model_checksum": result.model_checksum,
    }

    try:
        export_svc = DICOMExportService()
        exported = await export_svc.export_result(
            study_instance_uid=result.study_instance_uid,
            usecase_name=result.usecase_name,
            result_data=result_data,
            export_sr=sr and settings.dicom_sr_enabled,
            export_seg=seg and settings.dicom_seg_enabled,
        )
    except Exception as exc:
        logger.error("dicom_export_endpoint_failed", result_id=result_id, error=str(exc))
        raise HTTPException(500, f"DICOM export failed: {str(exc)}")

    return {"result_id": result_id, "exported": exported}


def _to_response(result) -> ResultResponse:
    return ResultResponse(
        id=result.id,
        study_instance_uid=result.study_instance_uid,
        usecase_name=result.usecase_name,
        job_id=result.job_id,
        summary=result.summary,
        measurements=result.measurements,
        qa_flags=[f.value if hasattr(f, "value") else f for f in result.qa_flags],
        qa_details=result.qa_details,
        model_version=result.model_version,
        model_checksum=result.model_checksum,
        artifacts=[
            ArtifactResponse(
                name=a.name,
                artifact_type=a.artifact_type,
                storage_path=a.storage_path,
                content_type=a.content_type,
                size_bytes=a.size_bytes,
            )
            for a in result.artifacts
        ],
        version=getattr(result, "version", 1),
        is_latest=getattr(result, "is_latest", True),
        created_at=result.created_at,
    )
