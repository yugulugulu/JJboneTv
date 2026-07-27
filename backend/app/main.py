from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import Depends

from .config import settings
from .controllers.auth import router as auth_router
from .controllers.model_catalog import router as model_catalog_router
from .controllers.project_domain import router as project_domain_router
from .database import get_db
from .exceptions import (
    AccountDisabledError,
    DomainValidationError,
    IdempotencyConflictError,
    DuplicateFolderNameError,
    ResourceNotFoundError,
    RevisionConflictError,
)
from .runtime import validate_runtime_settings
from .middleware import AuthRateLimitMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings("user-api")
    yield


app = FastAPI(title="ThinkAI Studio API", version="0.2.0", lifespan=lifespan)
app.add_middleware(AuthRateLimitMiddleware)
app.include_router(auth_router)
app.include_router(project_domain_router)
app.include_router(model_catalog_router)


@app.exception_handler(AccountDisabledError)
def account_disabled(_: Request, __: AccountDisabledError):
    response = JSONResponse(
        status_code=401,
        content={"detail": "Account disabled", "code": "ACCOUNT_DISABLED"},
    )
    response.delete_cookie(
        key="access_token",
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@app.exception_handler(ResourceNotFoundError)
def resource_not_found(_: Request, __: ResourceNotFoundError):
    return JSONResponse(status_code=404, content={"detail": "Resource not found"})


@app.exception_handler(DuplicateFolderNameError)
def duplicate_folder(_: Request, __: DuplicateFolderNameError):
    return JSONResponse(status_code=409, content={"detail": "Folder name already exists"})


@app.exception_handler(RevisionConflictError)
def revision_conflict(_: Request, error: RevisionConflictError):
    return JSONResponse(
        status_code=409,
        content={"detail": "Project revision conflict", "current_revision": error.current_revision},
    )


@app.exception_handler(DomainValidationError)
def domain_validation(_: Request, error: DomainValidationError):
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.exception_handler(IdempotencyConflictError)
def idempotency_conflict(_: Request, __: IdempotencyConflictError):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Idempotency-Key 已用于不同的生成请求",
            "code": "IDEMPOTENCY_KEY_CONFLICT",
        },
    )


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "database": "connected",
        "model_api_configured": bool(settings.model_api_base_url and settings.model_api_key),
        "app_environment": settings.app_environment,
        "asset_storage_backend": settings.asset_storage_backend,
    }
