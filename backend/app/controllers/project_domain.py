import base64
import binascii
import json
import logging
from datetime import datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies.auth import get_current_user
from ..exceptions import (
    DomainValidationError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    RevisionConflictError,
)
from ..models import User
from ..schemas import (
    AssetCreate,
    AssetList,
    AssetRead,
    AssetUpdate,
    FolderCreate,
    FolderList,
    FolderRead,
    FolderUpdate,
    GenerationCreate,
    GenerationRead,
    ImageProjectGenerationCreate,
    ImageProjectGenerationRead,
    ImageEditRead,
    ProjectCreate,
    ProjectList,
    ProjectRead,
    ProjectGenerationList,
    ProjectUpdate,
    VersionList,
    WorkflowRead,
    WorkflowSave,
)
from ..services.asset_storage import (
    AssetStorageError,
    LocalAssetStorage,
    project_asset_path,
    storage_for_asset,
)
from ..services.project_domain import (
    ImageUploadStorageError,
    ImageUploadTooLargeError,
    InvalidImageError,
    InvalidMediaError,
    MediaUploadStorageError,
    MediaUploadTooLargeError,
    ProjectDomainService,
    UnsupportedMediaTypeError,
    UnsupportedImageTypeError,
)


router = APIRouter(prefix="/api", tags=["projects"])
logger = logging.getLogger(__name__)


def service(db: Session = Depends(get_db)) -> ProjectDomainService:
    return ProjectDomainService(db)


def generation_read(domain: ProjectDomainService, task) -> dict:
    return {
        "id": task.id,
        "project_id": task.project_id,
        "node_id": task.node_id,
        "input_revision": task.input_revision,
        "kind": task.kind,
        "model": task.model,
        "prompt": task.prompt,
        "parameters": task.parameters,
        "status": task.status,
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "outputs": [
            {"ordinal": output.ordinal, "asset": asset}
            for output, asset in domain.repo.generation_outputs(task.id)
        ],
    }


@router.get("/folders", response_model=FolderList)
def list_folders(
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return {"items": domain.list_folders(current_user.id)}


@router.post("/folders", response_model=FolderRead, status_code=status.HTTP_201_CREATED)
def create_folder(
    payload: FolderCreate,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.create_folder(current_user.id, payload.name)


@router.get("/folders/{folder_id}", response_model=FolderRead)
def get_folder(
    folder_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.get_folder(current_user.id, folder_id)


@router.patch("/folders/{folder_id}", response_model=FolderRead)
def update_folder(
    folder_id: UUID,
    payload: FolderUpdate,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.update_folder(current_user.id, folder_id, payload.name)


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    domain.delete_folder(current_user.id, folder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/folders/{folder_id}/restore", response_model=FolderRead)
def restore_folder(
    folder_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.restore_folder(current_user.id, folder_id)


@router.get("/projects", response_model=ProjectList)
def list_projects(
    folder_id: str | None = Query(default=None, description="Omit for all projects, use root for unfiled"),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    try:
        parsed_folder = None if folder_id in (None, "root") else UUID(folder_id)
        parsed_cursor = None
        if cursor is not None:
            raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii") + b"===").decode("utf-8"))
            parsed_cursor = (datetime.fromisoformat(raw[0]), UUID(raw[1]))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, binascii.Error):
        raise HTTPException(status_code=422, detail="Invalid folder_id or cursor") from None
    items = domain.list_projects(
        current_user.id,
        parsed_folder,
        root_only=folder_id == "root",
        limit=limit + 1,
        cursor=parsed_cursor,
    )
    next_cursor = None
    if len(items) > limit:
        items = items[:limit]
        last = items[-1]
        token = json.dumps([last.updated_at.isoformat(), str(last.id)]).encode("utf-8")
        next_cursor = base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
    items = domain.project_summaries(current_user.id, items)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.create_project(current_user.id, payload)


@router.post(
    "/image-project-generations",
    response_model=ImageProjectGenerationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_image_project_generation(
    payload: ImageProjectGenerationCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Idempotency-Key header is required",
                "code": "IDEMPOTENCY_KEY_REQUIRED",
            },
        )
    result = domain.create_image_project_generation(
        current_user.id, payload, idempotency_key
    )
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return {
        "project": result.project,
        "generation": generation_read(domain, result.task),
        "prompt_node_id": result.prompt_node_id,
        "image_node_id": result.image_node_id,
    }


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.get_project(current_user.id, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.update_project(current_user.id, project_id, payload)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    domain.delete_project(current_user.id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/restore", response_model=ProjectRead)
def restore_project(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.restore_project(current_user.id, project_id)


@router.get("/projects/{project_id}/workflow", response_model=WorkflowRead)
def get_workflow(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    project = domain.get_project(current_user.id, project_id)
    return {
        "workflow": project.workflow,
        "revision": project.current_revision,
        "updated_at": project.updated_at,
    }


@router.post(
    "/projects/{project_id}/assets/images",
    response_model=AssetRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_project_image(
    project_id: UUID,
    response: Response,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Idempotency-Key header is required",
                "code": "IDEMPOTENCY_KEY_REQUIRED",
            },
        )
    try:
        result = domain.upload_image_asset(
            current_user.id, project_id, file, idempotency_key
        )
    except ImageUploadTooLargeError:
        raise HTTPException(
            status_code=413,
            detail={
                "message": "Image exceeds the configured upload size limit",
                "code": "FILE_TOO_LARGE",
            },
        ) from None
    except UnsupportedImageTypeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "message": "Only JPEG, PNG, WebP, and GIF images are supported",
                "code": "UNSUPPORTED_IMAGE_TYPE",
            },
        ) from None
    except InvalidImageError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(error) or "Uploaded file is not a valid image",
                "code": "INVALID_IMAGE",
            },
        ) from None
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Idempotency-Key was already used for different image content",
                "code": "IDEMPOTENCY_KEY_CONFLICT",
            },
        ) from None
    except ImageUploadStorageError:
        logger.exception("Unable to store uploaded image for project %s", project_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Image storage is temporarily unavailable",
                "code": "UPLOAD_FAILED",
            },
        ) from None
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result.asset


@router.post(
    "/projects/{project_id}/assets/media",
    response_model=AssetRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_project_media(
    project_id: UUID,
    kind: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    """Stream a user media upload with type-specific limits.

    Images keep the existing specialized endpoint; this endpoint is intended for
    audio/video uploads and enforces 20/200/50 MiB limits server-side.
    """
    try:
        return domain.upload_media_asset(current_user.id, project_id, kind, file).asset
    except MediaUploadTooLargeError:
        limits = {"image": "20MB", "video": "200MB", "audio": "50MB"}
        raise HTTPException(
            status_code=413,
            detail={
                "message": f"{kind} exceeds the configured upload size limit ({limits.get(kind, 'configured limit')})",
                "code": "FILE_TOO_LARGE",
            },
        ) from None
    except UnsupportedMediaTypeError:
        expected = "MP3 or WAV audio file" if kind == "audio" else f"{kind}/* media file"
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"message": f"Expected a {expected}", "code": "UNSUPPORTED_MEDIA_TYPE"},
        ) from None
    except InvalidMediaError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(error), "code": "INVALID_MEDIA"},
        ) from None
    except MediaUploadStorageError:
        logger.exception("Unable to store uploaded %s for project %s", kind, project_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "Media storage is temporarily unavailable", "code": "UPLOAD_FAILED"},
        ) from None
    except DomainValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(error), "code": "INVALID_UPLOAD"},
        ) from None


@router.post(
    "/projects/{project_id}/assets/{kind}",
    response_model=AssetRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_project_media_by_kind(
    project_id: UUID,
    kind: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    """Path-based alias accepting both singular and plural media names."""
    normalized_kind = {"images": "image", "videos": "video", "audios": "audio"}.get(
        kind.strip().lower(), kind.strip().lower()
    )
    return upload_project_media(project_id, normalized_kind, file, current_user, domain)


@router.post(
    "/projects/{project_id}/nodes/{node_id}/image-edit",
    response_model=ImageEditRead,
    status_code=status.HTTP_201_CREATED,
)
def save_project_image_edit(
    project_id: UUID,
    node_id: UUID,
    response: Response,
    file: UploadFile = File(...),
    base_revision: int = Form(..., ge=0),
    expected_asset_id: UUID = Form(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Idempotency-Key header is required",
                "code": "IDEMPOTENCY_KEY_REQUIRED",
            },
        )
    try:
        result = domain.save_image_edit(
            current_user.id,
            project_id,
            node_id,
            file,
            base_revision=base_revision,
            expected_asset_id=expected_asset_id,
            idempotency_key=idempotency_key,
        )
    except ImageUploadTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"message": "Image exceeds the configured upload size limit", "code": "FILE_TOO_LARGE"},
        ) from None
    except UnsupportedImageTypeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"message": "Only JPEG, PNG, WebP, and GIF images are supported", "code": "UNSUPPORTED_IMAGE_TYPE"},
        ) from None
    except InvalidImageError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(error) or "Edited file is not a valid image", "code": "INVALID_IMAGE"},
        ) from None
    except RevisionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Project revision conflict",
                "code": "REVISION_CONFLICT",
                "current_revision": error.current_revision,
            },
        ) from None
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Idempotency-Key was already used for a different image edit",
                "code": "IDEMPOTENCY_KEY_CONFLICT",
            },
        ) from None
    except ImageUploadStorageError:
        logger.exception("Unable to store edited image for project %s", project_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "Image storage is temporarily unavailable", "code": "UPLOAD_FAILED"},
        ) from None
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return {
        "asset": result.asset,
        "workflow": result.workflow,
        "revision": result.revision,
        "updated_at": result.updated_at,
        "replayed": result.replayed,
    }


@router.put("/projects/{project_id}/workflow", response_model=WorkflowRead)
def save_workflow(
    project_id: UUID,
    payload: WorkflowSave,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    project = domain.save_workflow(current_user.id, project_id, payload)
    return {
        "workflow": project.workflow,
        "revision": project.current_revision,
        "updated_at": project.updated_at,
    }


@router.get("/projects/{project_id}/versions", response_model=VersionList)
def list_versions(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return {"items": domain.list_versions(current_user.id, project_id)}


@router.post("/projects/{project_id}/versions/{version_id}/restore", response_model=WorkflowRead)
def restore_version(
    project_id: UUID,
    version_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    project = domain.restore_version(current_user.id, project_id, version_id)
    return {
        "workflow": project.workflow,
        "revision": project.current_revision,
        "updated_at": project.updated_at,
    }


@router.get("/assets", response_model=AssetList)
def list_assets(
    project_id: UUID,
    kind: str | None = Query(default=None, pattern="^(text|image|video|audio)$"),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return {"items": domain.list_assets(current_user.id, project_id, kind)}


@router.post("/assets", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
def create_asset(
    payload: AssetCreate,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.create_asset(current_user.id, payload)


@router.get("/assets/{asset_id}", response_model=AssetRead)
def get_asset(
    asset_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.get_asset(current_user.id, asset_id)


@router.get("/assets/{asset_id}/content")
def get_asset_content(
    asset_id: UUID,
    download: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    asset = domain.get_asset(current_user.id, asset_id)
    if asset.kind == "text" or asset.status != "ready" or not asset.storage_key:
        raise ResourceNotFoundError
    try:
        storage = storage_for_asset(asset.asset_metadata)
        storage.validate_project_key(asset.storage_key, asset.owner_id, asset.project_id)
        if not isinstance(storage, LocalAssetStorage):
            filename = (asset.original_name or f"{asset.id}") if download else None
            url = storage.signed_get_url(asset.storage_key, filename)
            return RedirectResponse(
                url,
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        path = project_asset_path(asset.storage_key, asset.owner_id, asset.project_id)
    except ValueError:
        raise ResourceNotFoundError from None
    except AssetStorageError:
        logger.exception("Unable to access storage for asset %s", asset.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Asset storage is temporarily unavailable",
        ) from None
    if not path.is_file():
        raise ResourceNotFoundError
    return FileResponse(
        path,
        media_type=asset.mime_type,
        filename=(asset.original_name or f"{asset.id}{path.suffix}") if download else None,
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/assets/{asset_id}/signed-url")
def get_asset_signed_url(
    asset_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    """Return a short-lived, authenticated URL for copying an OSS asset link.

    The storage key is intentionally kept server-side.  Local development
    storage falls back to the authenticated content endpoint.
    """

    asset = domain.get_asset(current_user.id, asset_id)
    if asset.kind == "text" or asset.status != "ready" or not asset.storage_key:
        raise ResourceNotFoundError
    try:
        storage = storage_for_asset(asset.asset_metadata)
        storage.validate_project_key(asset.storage_key, asset.owner_id, asset.project_id)
        metadata = asset.asset_metadata if isinstance(asset.asset_metadata, dict) else {}
        if isinstance(storage, LocalAssetStorage) or metadata.get("storage_backend") == "local":
            return {"url": f"/api/assets/{asset.id}/content"}
        return {"url": storage.signed_get_url(asset.storage_key)}
    except ValueError:
        raise ResourceNotFoundError from None
    except AssetStorageError:
        logger.exception("Unable to create signed URL for asset %s", asset.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Asset storage is temporarily unavailable",
        ) from None


@router.patch("/assets/{asset_id}", response_model=AssetRead)
def update_asset(
    asset_id: UUID,
    payload: AssetUpdate,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    return domain.update_asset(current_user.id, asset_id, payload)


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(
    asset_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    domain.delete_asset(current_user.id, asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/generations", response_model=GenerationRead, status_code=status.HTTP_202_ACCEPTED)
def create_generation(
    payload: GenerationCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Idempotency-Key header is required",
                "code": "IDEMPOTENCY_KEY_REQUIRED",
            },
        )
    result = domain.create_generation(current_user.id, payload, idempotency_key)
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result.task


@router.get("/generations/{task_id}", response_model=GenerationRead)
def get_generation(
    task_id: UUID,
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    task = domain.get_generation(current_user.id, task_id)
    return generation_read(domain, task)


@router.get("/projects/{project_id}/generations", response_model=ProjectGenerationList)
def list_project_generations(
    project_id: UUID,
    latest_per_node: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
    domain: ProjectDomainService = Depends(service),
):
    tasks = domain.list_project_generations(
        current_user.id, project_id, latest_per_node=latest_per_node
    )
    return {"items": [generation_read(domain, task) for task in tasks]}
