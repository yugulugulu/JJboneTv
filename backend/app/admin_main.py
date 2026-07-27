from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .controllers.admin import router as admin_router
from .database import SessionLocal, get_db
from .exceptions import (
    ArchivedModelError,
    DomainValidationError,
    DuplicateModelGroupNameError,
    DuplicateModelKeyError,
    ModelGroupNotActiveError,
    ModelGroupNotEmptyError,
    ModelInUseError,
    ModelKindMismatchError,
    ResourceNotFoundError,
)
from .services.admin import AdminService
from .runtime import validate_runtime_settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings("admin-api")
    if settings.admin_bootstrap_enabled:
        with SessionLocal() as db:
            AdminService(db).bootstrap(
                settings.admin_bootstrap_email,
                settings.admin_bootstrap_password,
            )
    yield


app = FastAPI(title="ThinkTV Admin API", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)


@app.exception_handler(DomainValidationError)
def domain_validation(_: Request, error: DomainValidationError):
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.exception_handler(ResourceNotFoundError)
def resource_not_found(_: Request, __: ResourceNotFoundError):
    return JSONResponse(status_code=404, content={"detail": "Resource not found"})


@app.exception_handler(DuplicateModelKeyError)
def duplicate_model_key(_: Request, __: DuplicateModelKeyError):
    return JSONResponse(status_code=409, content={"detail": "Model key already exists"})


@app.exception_handler(DuplicateModelGroupNameError)
def duplicate_model_group_name(_: Request, __: DuplicateModelGroupNameError):
    return JSONResponse(status_code=409, content={"detail": "Model group name already exists"})


@app.exception_handler(ArchivedModelError)
def archived_model(_: Request, __: ArchivedModelError):
    return JSONResponse(status_code=409, content={"detail": "Archived models cannot be changed"})


@app.exception_handler(ModelInUseError)
def model_in_use(_: Request, __: ModelInUseError):
    return JSONResponse(
        status_code=409,
        content={"detail": "Model has queued or running generation tasks", "code": "MODEL_IN_USE"},
    )


@app.exception_handler(ModelGroupNotEmptyError)
def model_group_not_empty(_: Request, __: ModelGroupNotEmptyError):
    return JSONResponse(
        status_code=409,
        content={"detail": "Model group still contains models", "code": "GROUP_NOT_EMPTY"},
    )


@app.exception_handler(ModelGroupNotActiveError)
def model_group_not_active(_: Request, __: ModelGroupNotActiveError):
    return JSONResponse(
        status_code=409,
        content={"detail": "Model group must be active", "code": "MODEL_GROUP_NOT_ACTIVE"},
    )


@app.exception_handler(ModelKindMismatchError)
def model_kind_mismatch(_: Request, __: ModelKindMismatchError):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Only text models can be changed through this endpoint",
            "code": "MODEL_KIND_MISMATCH",
        },
    )


@app.get("/admin-api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}
