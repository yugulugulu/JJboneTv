from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ..admin_schemas import (
    AdminAuthResponse,
    AdminLogin,
    AdminModelCreate,
    AdminModelGroupCreate,
    AdminModelGroupList,
    AdminModelGroupRead,
    AdminModelGroupUpdate,
    AdminModelList,
    AdminModelRead,
    AdminModelUpdate,
    AdminTextModelCreate,
    AdminTextModelUpdate,
    AdminUserList,
    AdminUserRead,
    AdminUserStatusUpdate,
)
from ..config import settings
from ..core.admin_security import create_admin_access_token
from ..database import get_db
from ..dependencies.admin_auth import get_current_admin
from ..exceptions import InvalidCredentialsError
from ..models import AdminAccount
from ..services.admin import AdminService


router = APIRouter(prefix="/admin-api", tags=["admin"])


def service(db: Session = Depends(get_db)) -> AdminService:
    return AdminService(db)


def set_admin_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="thinktv_admin_session",
        value=token,
        max_age=settings.admin_jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/auth/login", response_model=AdminAuthResponse)
def login(
    payload: AdminLogin,
    response: Response,
    domain: AdminService = Depends(service),
):
    try:
        admin = domain.login(str(payload.email), payload.password)
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrator credentials",
        ) from None
    set_admin_cookie(response, create_admin_access_token(admin.id))
    return {"admin": admin}


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key="thinktv_admin_session",
        path="/",
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/auth/me", response_model=AdminAuthResponse)
def me(admin: AdminAccount = Depends(get_current_admin)):
    return {"admin": admin}


@router.get("/users", response_model=AdminUserList)
def list_users(
    search: str | None = Query(default=None, max_length=320),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    _: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    items, total = domain.list_users(
        search=search,
        page=page,
        page_size=page_size,
        sort_order=sort_order,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/users/{user_id}", response_model=AdminUserRead)
def get_user(
    user_id: UUID,
    _: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.get_user(user_id)


@router.patch("/users/{user_id}/status", response_model=AdminUserRead)
def update_user_status(
    user_id: UUID,
    payload: AdminUserStatusUpdate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.update_user_status(admin.id, user_id, payload)


@router.get("/models", response_model=AdminModelList)
def list_models(
    _: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return {"items": domain.list_models()}


@router.get("/model-groups", response_model=AdminModelGroupList)
def list_model_groups(
    _: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return {"items": domain.list_model_groups()}


@router.post(
    "/model-groups", response_model=AdminModelGroupRead, status_code=status.HTTP_201_CREATED
)
def create_model_group(
    payload: AdminModelGroupCreate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.create_model_group(admin.id, payload)


@router.patch("/model-groups/{group_id}", response_model=AdminModelGroupRead)
def update_model_group(
    group_id: UUID,
    payload: AdminModelGroupUpdate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.update_model_group(admin.id, group_id, payload)


@router.delete("/model-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_group(
    group_id: UUID,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    domain.delete_model_group(admin.id, group_id)


@router.post("/models", response_model=AdminModelRead, status_code=status.HTTP_201_CREATED)
def create_model(
    payload: AdminModelCreate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.create_model(admin.id, payload)


@router.post("/text-models", response_model=AdminModelRead, status_code=status.HTTP_201_CREATED)
def create_text_model(
    payload: AdminTextModelCreate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.create_text_model(admin.id, payload)


@router.patch("/text-models/{model_id}", response_model=AdminModelRead)
def update_text_model(
    model_id: UUID,
    payload: AdminTextModelUpdate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.update_text_model(admin.id, model_id, payload)


@router.get("/models/{model_id}", response_model=AdminModelRead)
def get_model(
    model_id: UUID,
    _: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.get_model(model_id)


@router.patch("/models/{model_id}", response_model=AdminModelRead)
def update_model(
    model_id: UUID,
    payload: AdminModelUpdate,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    return domain.update_model(admin.id, model_id, payload)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(
    model_id: UUID,
    admin: AdminAccount = Depends(get_current_admin),
    domain: AdminService = Depends(service),
):
    domain.delete_model(admin.id, model_id)
