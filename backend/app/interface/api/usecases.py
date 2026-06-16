from typing import Annotated

from fastapi import APIRouter, Depends

from app.application.usecase_registry import UseCaseRegistry
from app.interface.api.dependencies import get_registry
from app.interface.schemas.usecase import UseCaseListResponse, UseCaseResponse

router = APIRouter(prefix="/usecases", tags=["usecases"])


@router.get("", response_model=UseCaseListResponse)
async def list_usecases(
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    usecases = registry.usecases
    return UseCaseListResponse(
        usecases=[
            UseCaseResponse(
                name=uc.name,
                version=uc.version,
                description=uc.description,
                supported_body_parts=uc.supported_body_parts,
                required_sequences=uc.required_sequences,
                model_type=uc.model_type,
                enabled=uc.enabled,
                module_path=uc.module_path,
                registered_at=uc.registered_at,
            )
            for uc in usecases.values()
        ]
    )


@router.get("/{usecase_name}/ui-schema")
async def get_ui_schema(
    usecase_name: str,
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    schema = registry.get_ui_schema(usecase_name)
    if not schema:
        from fastapi import HTTPException
        raise HTTPException(404, f"UI schema not found for {usecase_name}")
    return schema


@router.get("/{usecase_name}/output-schema")
async def get_output_schema(
    usecase_name: str,
    registry: Annotated[UseCaseRegistry, Depends(get_registry)],
):
    schema = registry.get_output_schema(usecase_name)
    if not schema:
        from fastapi import HTTPException
        raise HTTPException(404, f"Output schema not found for {usecase_name}")
    return schema
