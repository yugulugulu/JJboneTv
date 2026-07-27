from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies.auth import get_current_user
from ..model_schemas import (
    UserModelCredentialRead,
    UserModelCredentialWrite,
    UserModelGroupList,
    UserModelList,
)
from ..models import User
from ..schemas import PromptOptimizationCreate, PromptOptimizationRead
from ..services.model_catalog import ModelCatalogService
from ..services.prompt_optimization import (
    PromptOptimizationProviderError,
    PromptOptimizationService,
)


router = APIRouter(prefix="/api", tags=["model-catalog"])


def service(db: Session = Depends(get_db)) -> ModelCatalogService:
    return ModelCatalogService(db)


def prompt_service(db: Session = Depends(get_db)) -> PromptOptimizationService:
    return PromptOptimizationService(db)


@router.get("/models", response_model=UserModelList)
def list_models(
    current_user: User = Depends(get_current_user),
    domain: ModelCatalogService = Depends(service),
):
    return {"items": domain.list_available_models(current_user.id)}


@router.get("/model-groups", response_model=UserModelGroupList)
def list_model_groups(
    current_user: User = Depends(get_current_user),
    domain: ModelCatalogService = Depends(service),
):
    return {"items": domain.list_groups(current_user.id)}


@router.put("/model-groups/{group_id}/credential", response_model=UserModelCredentialRead)
def save_model_group_credential(
    group_id: UUID,
    payload: UserModelCredentialWrite,
    current_user: User = Depends(get_current_user),
    domain: ModelCatalogService = Depends(service),
):
    return domain.save_credential(current_user.id, group_id, payload.api_key)


@router.delete("/model-groups/{group_id}/credential", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_group_credential(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ModelCatalogService = Depends(service),
):
    domain.delete_credential(current_user.id, group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/prompt-optimizations", response_model=PromptOptimizationRead)
def optimize_prompt(
    payload: PromptOptimizationCreate,
    current_user: User = Depends(get_current_user),
    domain: PromptOptimizationService = Depends(prompt_service),
):
    try:
        optimized_prompt = domain.optimize(
            current_user.id, payload.model_id, payload.target_kind, payload.input
        )
    except PromptOptimizationProviderError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None
    return {
        "model_id": payload.model_id,
        "target_kind": payload.target_kind,
        "optimized_prompt": optimized_prompt,
    }
