import hashlib
import json
import logging
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..exceptions import (
    DomainValidationError,
    DuplicateFolderNameError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    RevisionConflictError,
)
from ..database import SessionLocal
from ..models import Asset, AssetEditRequest, Folder, GenerationTask, Project, ProjectVersion
from ..repositories.project_domain import ProjectDomainRepository
from .model_catalog import ModelCatalogService
from .asset_storage import (
    AssetStorageError,
    generation_asset_storage,
    project_asset_path,
    storage_metadata,
)
from .storage_deletion_jobs import (
    enqueue_asset_deletion,
    enqueue_storage_snapshot_deletion,
)
from .generation_provider import (
    INPUT_REFERENCES_PARAMETER,
    effective_generation_prompt,
    multipart_scalar_parameters,
    normalize_generation_parameters,
    stored_generation_parameters,
    validate_provider_input_support,
    validate_required_provider_prompt,
)
from ..schemas import (
    AssetCreate,
    AssetUpdate,
    GenerationCreate,
    ImageProjectGenerationCreate,
    ProjectCreate,
    ProjectListItem,
    ProjectUpdate,
    Workflow,
    WORKFLOW_INPUT_KINDS,
    WorkflowSave,
)


def _workflow_dict(workflow: Workflow) -> dict:
    return workflow.model_dump(mode="json")


def _validate_workflow_write_constraints(workflow: Workflow) -> None:
    nodes_by_id = {node.id: node for node in workflow.nodes}
    image_text_predecessors: dict[UUID, int] = {}
    image_image_predecessors: dict[UUID, int] = {}
    for edge in workflow.edges:
        source = nodes_by_id[edge.source_node_id]
        target = nodes_by_id[edge.target_node_id]
        if target.type != "image":
            continue
        if source.type == "prompt":
            predecessor_count = image_text_predecessors.get(target.id, 0) + 1
            if predecessor_count > 1:
                raise DomainValidationError(
                    "图片节点最多只能有一个文本前置节点，请删除多余的文本连线后再保存"
                )
            image_text_predecessors[target.id] = predecessor_count
        elif source.type == "image":
            predecessor_count = image_image_predecessors.get(target.id, 0) + 1
            if predecessor_count > 5:
                raise DomainValidationError(
                    "图片节点最多只能有五个图片前置节点，请删除多余的图片连线后再保存"
                )
            image_image_predecessors[target.id] = predecessor_count


def _snapshot_hash(snapshot: dict) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generation_request_hash(payload: GenerationCreate) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def image_project_prompt_node_id(project: Project, task: GenerationTask) -> UUID:
    """Recover the prompt node for an idempotent homepage generation replay.

    The workflow edge is the primary source of truth. The frozen task inputs are a
    defensive fallback so an old or manually repaired workflow cannot turn a safe
    idempotent retry into an unhandled ``StopIteration`` error.
    """

    for edge in project.workflow.get("edges", []):
        if str(edge.get("target_node_id")) != str(task.node_id):
            continue
        try:
            return UUID(str(edge["source_node_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    references = task.parameters.get(INPUT_REFERENCES_PARAMETER, [])
    if isinstance(references, list):
        for reference in references:
            if not isinstance(reference, dict) or reference.get("kind") != "text":
                continue
            try:
                return UUID(str(reference["node_id"]))
            except (KeyError, TypeError, ValueError):
                continue
    raise DomainValidationError("Idempotent image project generation data is inconsistent")


logger = logging.getLogger(__name__)

IMAGE_UPLOAD_CHUNK_BYTES = 1024 * 1024
IMAGE_UPLOAD_MAX_PIXELS = 100_000_000
IMAGE_UPLOAD_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}


class ImageUploadTooLargeError(Exception):
    pass


class UnsupportedImageTypeError(Exception):
    pass


class InvalidImageError(Exception):
    pass


class ImageUploadStorageError(Exception):
    pass


@dataclass(frozen=True)
class SpoolImageResult:
    path: Path
    byte_size: int
    checksum_sha256: str
    image_format: str
    mime_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class ImageUploadCreationResult:
    asset: Asset
    replayed: bool


@dataclass(frozen=True)
class ImageEditCreationResult:
    asset: Asset
    workflow: dict
    revision: int
    updated_at: datetime
    replayed: bool


@dataclass(frozen=True)
class GenerationCreationResult:
    task: GenerationTask
    replayed: bool


@dataclass(frozen=True)
class ImageProjectGenerationCreationResult:
    project: Project
    task: GenerationTask
    prompt_node_id: UUID
    image_node_id: UUID
    replayed: bool


class ProjectDomainService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = ProjectDomainRepository(db)

    def _commit(self) -> None:
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            if "uq_folders_owner_active_name_ci" in str(error.orig):
                raise DuplicateFolderNameError from error
            if "uq_asset_edit_requests_owner_idempotency" in str(error.orig):
                raise IdempotencyConflictError from error
            raise DomainValidationError("Data violates a project constraint") from error

    @staticmethod
    def _queue_upload_compensation(
        *,
        owner_id: UUID,
        project_id: UUID,
        storage_key: str,
        metadata: dict,
    ) -> None:
        try:
            with SessionLocal.begin() as cleanup_db:
                enqueue_storage_snapshot_deletion(
                    cleanup_db,
                    owner_id=owner_id,
                    project_id=project_id,
                    storage_key=storage_key,
                    metadata=metadata,
                    reason="upload_compensation",
                )
        except Exception:
            logger.exception(
                "Unable to persist cleanup job for uploaded object: %s", storage_key
            )

    def list_folders(self, owner_id: UUID) -> list[Folder]:
        return self.repo.list_folders(owner_id)

    def get_folder(self, owner_id: UUID, folder_id: UUID) -> Folder:
        folder = self.repo.get_folder(folder_id, owner_id)
        if folder is None:
            raise ResourceNotFoundError
        return folder

    def create_folder(self, owner_id: UUID, name: str) -> Folder:
        folder = Folder(owner_id=owner_id, name=name)
        self.db.add(folder)
        self._commit()
        self.db.refresh(folder)
        return folder

    def update_folder(self, owner_id: UUID, folder_id: UUID, name: str) -> Folder:
        folder = self.repo.get_folder(folder_id, owner_id)
        if folder is None:
            raise ResourceNotFoundError
        folder.name = name
        self._commit()
        self.db.refresh(folder)
        return folder

    def delete_folder(self, owner_id: UUID, folder_id: UUID) -> None:
        folder = self.repo.lock_folder(folder_id, owner_id)
        if folder is None:
            raise ResourceNotFoundError
        now = datetime.now(timezone.utc)
        batch_id = uuid.uuid4()
        folder.deleted_at = now
        folder.trash_batch_id = batch_id
        self.db.execute(
            update(Project)
            .where(
                Project.owner_id == owner_id,
                Project.folder_id == folder.id,
                Project.deleted_at.is_(None),
            )
            .values(deleted_at=now, trash_batch_id=batch_id)
        )
        self._commit()

    def restore_folder(self, owner_id: UUID, folder_id: UUID) -> Folder:
        folder = self.repo.get_folder(folder_id, owner_id, deleted=True)
        if folder is None:
            raise ResourceNotFoundError
        batch_id = folder.trash_batch_id
        if batch_id is not None:
            self.db.execute(
                update(Project)
                .where(
                    Project.owner_id == owner_id,
                    Project.folder_id == folder.id,
                    Project.trash_batch_id == batch_id,
                    Project.deleted_at.is_not(None),
                )
                .values(deleted_at=None, trash_batch_id=None)
            )
        folder.deleted_at = None
        folder.trash_batch_id = None
        self._commit()
        self.db.refresh(folder)
        return folder

    def _require_folder(self, owner_id: UUID, folder_id: UUID | None, *, lock: bool = False) -> None:
        if folder_id is not None and (
            self.repo.lock_folder(folder_id, owner_id)
            if lock
            else self.repo.get_folder(folder_id, owner_id)
        ) is None:
            raise ResourceNotFoundError

    def list_projects(
        self,
        owner_id: UUID,
        folder_id: UUID | None,
        root_only: bool,
        limit: int,
        cursor: tuple[datetime, UUID] | None,
    ) -> list[Project]:
        if folder_id is not None:
            self._require_folder(owner_id, folder_id)
        return self.repo.list_projects(
            owner_id, folder_id, root_only=root_only, limit=limit, cursor=cursor
        )

    def project_summaries(
        self, owner_id: UUID, projects: list[Project]
    ) -> list[ProjectListItem]:
        preview_asset_ids = self.repo.first_ready_image_asset_ids(
            owner_id, [project.id for project in projects]
        )
        return [
            ProjectListItem.model_validate(project).model_copy(
                update={"preview_asset_id": preview_asset_ids.get(project.id)}
            )
            for project in projects
        ]

    def create_project(self, owner_id: UUID, payload: ProjectCreate) -> Project:
        self._require_folder(owner_id, payload.folder_id, lock=True)
        if payload.workflow is not None:
            _validate_workflow_write_constraints(payload.workflow)
        workflow = _workflow_dict(payload.workflow) if payload.workflow is not None else None
        project = Project(
            owner_id=owner_id,
            folder_id=payload.folder_id,
            name=payload.name,
            project_type=payload.project_type,
            **(
                {
                    "workflow": workflow,
                    "workflow_schema_version": payload.workflow.schema_version,
                    "current_revision": 1,
                }
                if payload.workflow is not None
                else {}
            ),
        )
        self.db.add(project)
        self.db.flush()
        if workflow is not None:
            self._validate_workflow_references(owner_id, project.id, payload.workflow)
            self.db.add(
                ProjectVersion(
                    project_id=project.id,
                    created_by_user_id=owner_id,
                    version_no=1,
                    schema_version=payload.workflow.schema_version,
                    snapshot=workflow,
                    snapshot_hash=_snapshot_hash(workflow),
                    reason="initial",
                )
            )
        self._commit()
        self.db.refresh(project)
        return project

    def get_project(self, owner_id: UUID, project_id: UUID) -> Project:
        project = self.repo.get_project(project_id, owner_id)
        if project is None:
            raise ResourceNotFoundError
        return project

    def update_project(self, owner_id: UUID, project_id: UUID, payload: ProjectUpdate) -> Project:
        project = self.get_project(owner_id, project_id)
        if payload.name is not None:
            project.name = payload.name
        if "folder_id" in payload.model_fields_set:
            self._require_folder(owner_id, payload.folder_id, lock=True)
            project.folder_id = payload.folder_id
        if payload.project_type is not None:
            project.project_type = payload.project_type
        if "cover_asset_id" in payload.model_fields_set:
            if payload.cover_asset_id is not None:
                asset = self.repo.get_asset(payload.cover_asset_id, owner_id)
                if asset is None or asset.project_id != project.id:
                    raise ResourceNotFoundError
                if asset.kind != "image" or asset.status != "ready":
                    raise DomainValidationError("Project cover must be a ready image asset in this project")
            project.cover_asset_id = payload.cover_asset_id
        self._commit()
        self.db.refresh(project)
        return project

    def delete_project(self, owner_id: UUID, project_id: UUID) -> None:
        project = self.get_project(owner_id, project_id)
        project.deleted_at = datetime.now(timezone.utc)
        project.trash_batch_id = uuid.uuid4()
        self._commit()

    def restore_project(self, owner_id: UUID, project_id: UUID) -> Project:
        project = self.repo.get_project(project_id, owner_id, deleted=True)
        if project is None:
            raise ResourceNotFoundError
        if project.folder_id is not None and self.repo.get_folder(project.folder_id, owner_id) is None:
            raise DomainValidationError("Restore the containing folder before restoring this project")
        project.deleted_at = None
        project.trash_batch_id = None
        self._commit()
        self.db.refresh(project)
        return project

    def _validate_workflow_references(self, owner_id: UUID, project_id: UUID, workflow: Workflow) -> None:
        asset_ids = {node.data.asset_id for node in workflow.nodes if node.data.asset_id is not None}
        task_ids = {
            task_id
            for node in workflow.nodes
            for task_id in [node.data.generation_task_id]
            if task_id is not None
        }
        task_ids.update(
            message.generation_task_id
            for node in workflow.nodes
            for message in node.data.messages
            if message.generation_task_id is not None
        )
        if self.repo.owned_project_assets(asset_ids, owner_id, project_id) != asset_ids:
            raise DomainValidationError("Workflow references an unavailable asset")
        if self.repo.owned_project_generations(task_ids, owner_id, project_id) != task_ids:
            raise DomainValidationError("Workflow references an unavailable generation task")

    def save_workflow(self, owner_id: UUID, project_id: UUID, payload: WorkflowSave) -> Project:
        project = self.repo.lock_project(project_id, owner_id)
        if project is None:
            raise ResourceNotFoundError
        if project.current_revision != payload.base_revision:
            raise RevisionConflictError(project.current_revision)
        _validate_workflow_write_constraints(payload.workflow)
        self._validate_workflow_references(owner_id, project_id, payload.workflow)
        snapshot = _workflow_dict(payload.workflow)
        saved = self.repo.save_workflow_atomic(
            project_id, owner_id, payload.base_revision, snapshot, payload.workflow.schema_version
        )
        if saved is None:
            self.db.rollback()
            current = self.repo.get_project(project_id, owner_id)
            if current is None:
                raise ResourceNotFoundError
            raise RevisionConflictError(current.current_revision)
        revision, _ = saved
        reason = "initial" if payload.base_revision == 0 else payload.reason
        if (
            reason == "interval"
            and (latest := self.repo.latest_version(project_id)) is not None
            and latest.created_at >= datetime.now(timezone.utc) - timedelta(minutes=5)
        ):
            reason = None
        if reason is not None:
            latest = self.repo.latest_version(project_id)
            digest = _snapshot_hash(snapshot)
            if latest is None or latest.snapshot_hash != digest:
                self.db.add(
                    ProjectVersion(
                        project_id=project_id,
                        created_by_user_id=owner_id,
                        version_no=revision,
                        schema_version=payload.workflow.schema_version,
                        snapshot=snapshot,
                        snapshot_hash=digest,
                        reason=reason,
                    )
                )
        self.repo.prune_versions(
            project_id, datetime.now(timezone.utc) - timedelta(days=90), keep_latest=100
        )
        self._commit()
        return self.get_project(owner_id, project_id)

    def list_versions(self, owner_id: UUID, project_id: UUID) -> list[ProjectVersion]:
        self.get_project(owner_id, project_id)
        return self.repo.list_versions(project_id)

    def restore_version(self, owner_id: UUID, project_id: UUID, version_id: UUID) -> Project:
        project = self.get_project(owner_id, project_id)
        version = self.repo.get_version(version_id, project_id)
        if version is None:
            raise ResourceNotFoundError
        workflow = Workflow.model_validate(version.snapshot)
        _validate_workflow_write_constraints(workflow)
        asset_ids = {node.data.asset_id for node in workflow.nodes if node.data.asset_id is not None}
        assets = self.repo.project_assets_including_deleted(asset_ids, owner_id, project_id)
        if {asset.id for asset in assets} != asset_ids:
            raise DomainValidationError("Historical workflow references an asset that was permanently removed")
        for asset in assets:
            if asset.deleted_at is not None:
                asset.deleted_at = None
                asset.status = "ready"
        task_ids = {
            task_id
            for node in workflow.nodes
            for task_id in [node.data.generation_task_id]
            if task_id is not None
        }
        task_ids.update(
            message.generation_task_id
            for node in workflow.nodes
            for message in node.data.messages
            if message.generation_task_id is not None
        )
        if self.repo.owned_project_generations(task_ids, owner_id, project_id) != task_ids:
            raise DomainValidationError("Historical workflow references an unavailable generation task")
        saved = self.repo.save_workflow_atomic(
            project_id,
            owner_id,
            project.current_revision,
            version.snapshot,
            version.schema_version,
        )
        if saved is None:
            self.db.rollback()
            current = self.repo.get_project(project_id, owner_id)
            if current is None:
                raise ResourceNotFoundError
            raise RevisionConflictError(current.current_revision)
        revision, _ = saved
        self.db.add(
            ProjectVersion(
                project_id=project_id,
                created_by_user_id=owner_id,
                version_no=revision,
                schema_version=version.schema_version,
                snapshot=version.snapshot,
                snapshot_hash=_snapshot_hash(version.snapshot),
                reason="restore",
                restored_from_version_id=version.id,
            )
        )
        self.repo.prune_versions(
            project_id, datetime.now(timezone.utc) - timedelta(days=90), keep_latest=100
        )
        self._commit()
        return self.get_project(owner_id, project_id)

    def list_assets(self, owner_id: UUID, project_id: UUID, kind: str | None) -> list[Asset]:
        self.get_project(owner_id, project_id)
        return self.repo.list_assets(project_id, owner_id, kind)

    def create_asset(self, owner_id: UUID, payload: AssetCreate) -> Asset:
        self.get_project(owner_id, payload.project_id)
        if payload.kind == "text":
            content = payload.text_content or ""
            byte_size = len(content.encode("utf-8"))
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            mime_type = payload.mime_type or "text/plain"
            status = payload.status or "ready"
        else:
            try:
                project_asset_path(payload.storage_key or "", owner_id, payload.project_id)
            except ValueError:
                raise DomainValidationError(
                    "Media storage key must belong to the authenticated user and project"
                ) from None
            byte_size = payload.byte_size or 0
            checksum = payload.checksum_sha256
            mime_type = payload.mime_type or "application/octet-stream"
            status = payload.status or "uploading"
        asset = Asset(
            owner_id=owner_id,
            project_id=payload.project_id,
            kind=payload.kind,
            source=payload.source,
            text_content=payload.text_content,
            original_name=payload.original_name,
            storage_key=payload.storage_key,
            mime_type=mime_type,
            byte_size=byte_size,
            width=payload.width,
            height=payload.height,
            duration_ms=payload.duration_ms,
            checksum_sha256=checksum,
            status=status,
            asset_metadata=payload.metadata,
        )
        self.db.add(asset)
        self._commit()
        self.db.refresh(asset)
        return asset

    @staticmethod
    def _validate_upload_idempotency_key(idempotency_key: str) -> str:
        normalized = idempotency_key.strip()
        if (
            not normalized
            or len(normalized) > 128
            or not normalized.isascii()
            or any(character.isspace() for character in normalized)
        ):
            raise DomainValidationError(
                "Idempotency-Key must contain 1 to 128 non-whitespace ASCII characters"
            )
        return normalized

    @staticmethod
    def _safe_upload_name(filename: str | None, extension: str) -> str:
        candidate = (filename or "").replace("\\", "/").split("/")[-1]
        candidate = candidate.replace("\x00", "").strip()
        return (candidate or f"uploaded-image{extension}")[:255]

    @staticmethod
    def _spool_and_inspect_image(upload: UploadFile) -> SpoolImageResult:
        maximum_bytes = settings.user_image_upload_max_bytes
        digest = hashlib.sha256()
        byte_size = 0
        temporary = tempfile.NamedTemporaryFile(prefix="thinktv-image-", suffix=".upload", delete=False)
        path = Path(temporary.name)
        try:
            try:
                upload.file.seek(0)
                while True:
                    chunk = upload.file.read(IMAGE_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > maximum_bytes:
                        raise ImageUploadTooLargeError
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
            finally:
                temporary.close()

            if byte_size == 0:
                raise InvalidImageError("Uploaded image is empty")

            try:
                with Image.open(path) as image:
                    image_format = (image.format or "").upper()
                    width, height = image.size
                    if image_format not in IMAGE_UPLOAD_FORMATS:
                        raise UnsupportedImageTypeError
                    if width <= 0 or height <= 0:
                        raise InvalidImageError("Uploaded image dimensions are invalid")
                    if width * height > IMAGE_UPLOAD_MAX_PIXELS:
                        raise InvalidImageError("Uploaded image exceeds the 100 megapixel limit")
                    image.verify()
            except UnsupportedImageTypeError:
                raise
            except InvalidImageError:
                raise
            except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
                raise InvalidImageError("Uploaded file is not a valid image") from error

            mime_type, extension = IMAGE_UPLOAD_FORMATS[image_format]
            return SpoolImageResult(
                path=path,
                byte_size=byte_size,
                checksum_sha256=digest.hexdigest(),
                image_format=image_format,
                mime_type=mime_type,
                extension=extension,
                width=width,
                height=height,
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def upload_image_asset(
        self,
        owner_id: UUID,
        project_id: UUID,
        upload: UploadFile,
        idempotency_key: str,
    ) -> ImageUploadCreationResult:
        normalized_key = self._validate_upload_idempotency_key(idempotency_key)
        spooled = self._spool_and_inspect_image(upload)
        storage_key: str | None = None
        storage = None
        try:
            project = self.repo.lock_project(project_id, owner_id)
            if project is None:
                raise ResourceNotFoundError

            idempotency_key_hash = hashlib.sha256(
                f"{owner_id}:{project_id}:{normalized_key}".encode("utf-8")
            ).hexdigest()
            existing = self.repo.get_image_upload_by_idempotency(
                owner_id, project_id, idempotency_key_hash
            )
            if existing is not None:
                if existing.checksum_sha256 != spooled.checksum_sha256:
                    raise IdempotencyConflictError
                self.db.commit()
                return ImageUploadCreationResult(existing, True)

            try:
                storage = generation_asset_storage()
                storage_key = storage.object_key(
                    f"{owner_id}/{project_id}/{uuid.uuid4()}{spooled.extension}"
                )
                storage.put_file(storage_key, spooled.path, spooled.mime_type)
            except (AssetStorageError, ValueError) as error:
                self.db.rollback()
                raise ImageUploadStorageError from error

            asset = Asset(
                owner_id=owner_id,
                project_id=project_id,
                kind="image",
                source="upload",
                text_content=None,
                original_name=self._safe_upload_name(upload.filename, spooled.extension),
                storage_key=storage_key,
                mime_type=spooled.mime_type,
                byte_size=spooled.byte_size,
                width=spooled.width,
                height=spooled.height,
                duration_ms=None,
                checksum_sha256=spooled.checksum_sha256,
                status="ready",
                asset_metadata={
                    **storage_metadata(storage),
                    "image_format": spooled.image_format,
                    "upload_idempotency_key_hash": idempotency_key_hash,
                },
            )
            self.db.add(asset)
            try:
                self._commit()
            except Exception:
                if storage_key is not None and storage is not None:
                    metadata = storage_metadata(storage)
                    try:
                        storage.delete_object(storage_key)
                    except AssetStorageError:
                        logger.exception(
                            "Unable to clean up uploaded object after database failure: %s",
                            storage_key,
                        )
                        self._queue_upload_compensation(
                            owner_id=owner_id,
                            project_id=project_id,
                            storage_key=storage_key,
                            metadata=metadata,
                        )
                raise
            self.db.refresh(asset)
            return ImageUploadCreationResult(asset, False)
        finally:
            spooled.path.unlink(missing_ok=True)

    def save_image_edit(
        self,
        owner_id: UUID,
        project_id: UUID,
        node_id: UUID,
        upload: UploadFile,
        *,
        base_revision: int,
        expected_asset_id: UUID,
        idempotency_key: str,
    ) -> ImageEditCreationResult:
        normalized_key = self._validate_upload_idempotency_key(idempotency_key)
        spooled = self._spool_and_inspect_image(upload)
        storage_key: str | None = None
        storage = None
        storage_snapshot: dict = {}
        persisted = False
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "owner_id": str(owner_id),
                    "project_id": str(project_id),
                    "node_id": str(node_id),
                    "base_revision": base_revision,
                    "expected_asset_id": str(expected_asset_id),
                    "checksum_sha256": spooled.checksum_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        def replay_existing_edit() -> ImageEditCreationResult | None:
            existing = self.repo.get_asset_edit_request(owner_id, normalized_key)
            if existing is None:
                return None
            if existing.request_hash != request_hash or existing.output_asset_id is None:
                raise IdempotencyConflictError
            project = self.repo.get_project(existing.project_id, owner_id)
            output = self.repo.get_asset(existing.output_asset_id, owner_id)
            if project is None or output is None:
                raise IdempotencyConflictError
            return ImageEditCreationResult(
                asset=output,
                workflow=project.workflow,
                revision=project.current_revision,
                updated_at=project.updated_at,
                replayed=True,
            )

        def cleanup_uploaded_object() -> None:
            nonlocal storage_key
            if storage_key is None or storage is None or persisted:
                return
            pending_key = storage_key
            storage_key = None
            try:
                storage.delete_object(pending_key)
            except AssetStorageError:
                logger.exception(
                    "Unable to clean up uncommitted edited image: %s", pending_key
                )
                self._queue_upload_compensation(
                    owner_id=owner_id,
                    project_id=project_id,
                    storage_key=pending_key,
                    metadata=storage_snapshot,
                )

        try:
            replay = replay_existing_edit()
            if replay is not None:
                return replay

            project = self.repo.lock_project(project_id, owner_id)
            if project is None:
                raise ResourceNotFoundError

            # A concurrent request may have committed while this transaction was
            # waiting for the project row lock. Re-read the durable idempotency
            # record before treating the advanced revision as a conflict.
            replay = replay_existing_edit()
            if replay is not None:
                return replay
            if project.current_revision != base_revision:
                raise RevisionConflictError(project.current_revision)

            workflow = deepcopy(project.workflow)
            target_node = next(
                (
                    node
                    for node in workflow.get("nodes", [])
                    if str(node.get("id")) == str(node_id)
                ),
                None,
            )
            if target_node is None or target_node.get("type") != "image":
                raise DomainValidationError("Image edit target must be an image node")
            node_data = target_node.get("data")
            if not isinstance(node_data, dict):
                raise DomainValidationError("Image node data is invalid")
            if str(node_data.get("asset_id")) != str(expected_asset_id):
                raise RevisionConflictError(project.current_revision)

            source_asset = self.repo.get_asset(expected_asset_id, owner_id)
            if (
                source_asset is None
                or source_asset.project_id != project_id
                or source_asset.kind != "image"
                or source_asset.status != "ready"
            ):
                raise DomainValidationError("Image edit source asset is unavailable")
            if (
                source_asset.width is None
                or source_asset.height is None
                or spooled.width != source_asset.width
                or spooled.height != source_asset.height
            ):
                raise DomainValidationError(
                    "Edited image dimensions must exactly match the source image"
                )

            try:
                storage = generation_asset_storage()
                storage_snapshot = storage_metadata(storage)
                storage_key = storage.object_key(
                    f"{owner_id}/{project_id}/{uuid.uuid4()}{spooled.extension}"
                )
                storage.put_file(storage_key, spooled.path, spooled.mime_type)
            except (AssetStorageError, ValueError) as error:
                self.db.rollback()
                raise ImageUploadStorageError from error

            output = Asset(
                owner_id=owner_id,
                project_id=project_id,
                kind="image",
                source="upload",
                text_content=None,
                original_name=self._safe_upload_name(upload.filename, spooled.extension),
                storage_key=storage_key,
                mime_type=spooled.mime_type,
                byte_size=spooled.byte_size,
                width=spooled.width,
                height=spooled.height,
                duration_ms=None,
                checksum_sha256=spooled.checksum_sha256,
                status="ready",
                asset_metadata={
                    **storage_snapshot,
                    "image_format": spooled.image_format,
                    "edited_from_asset_id": str(source_asset.id),
                    "edit_operation": "annotation",
                },
            )
            self.db.add(output)
            self.db.flush()
            node_data["asset_id"] = str(output.id)
            if project.cover_asset_id == source_asset.id:
                project.cover_asset_id = output.id

            saved = self.repo.save_workflow_atomic(
                project_id,
                owner_id,
                base_revision,
                workflow,
                int(workflow.get("schema_version") or 1),
            )
            if saved is None:
                raise RevisionConflictError(project.current_revision)
            revision, updated_at = saved
            self.db.add(
                ProjectVersion(
                    project_id=project_id,
                    created_by_user_id=owner_id,
                    version_no=revision,
                    schema_version=int(workflow.get("schema_version") or 1),
                    snapshot=workflow,
                    snapshot_hash=_snapshot_hash(workflow),
                    reason="manual",
                )
            )
            self.db.add(
                AssetEditRequest(
                    owner_id=owner_id,
                    project_id=project_id,
                    node_id=node_id,
                    idempotency_key=normalized_key,
                    request_hash=request_hash,
                    source_asset_id=source_asset.id,
                    output_asset_id=output.id,
                    project_revision=revision,
                )
            )
            self.repo.prune_versions(
                project_id, datetime.now(timezone.utc) - timedelta(days=90), keep_latest=100
            )
            self._commit()
            self.db.refresh(output)
            persisted = True
            return ImageEditCreationResult(
                asset=output,
                workflow=workflow,
                revision=revision,
                updated_at=updated_at,
                replayed=False,
            )
        except IdempotencyConflictError:
            cleanup_uploaded_object()
            # ``_commit`` rolls the failed transaction back. If the unique-key
            # conflict came from an equivalent concurrent request, return that
            # durable result; otherwise preserve the conflict response.
            replay = replay_existing_edit()
            if replay is not None:
                return replay
            raise
        except Exception:
            cleanup_uploaded_object()
            raise
        finally:
            spooled.path.unlink(missing_ok=True)

    def get_asset(self, owner_id: UUID, asset_id: UUID) -> Asset:
        asset = self.repo.get_asset(asset_id, owner_id)
        if asset is None:
            raise ResourceNotFoundError
        if self.repo.get_project(asset.project_id, owner_id) is None:
            raise ResourceNotFoundError
        return asset

    def update_asset(self, owner_id: UUID, asset_id: UUID, payload: AssetUpdate) -> Asset:
        asset = self.get_asset(owner_id, asset_id)
        if "text_content" in payload.model_fields_set:
            if asset.kind != "text" or payload.text_content is None:
                raise DomainValidationError("Only text assets can update text_content")
            asset.text_content = payload.text_content
            encoded = payload.text_content.encode("utf-8")
            asset.byte_size = len(encoded)
            asset.checksum_sha256 = hashlib.sha256(encoded).hexdigest()
        if "original_name" in payload.model_fields_set:
            asset.original_name = payload.original_name
        if payload.status is not None:
            asset.status = payload.status
        if payload.metadata is not None:
            asset.asset_metadata = payload.metadata
        self._commit()
        self.db.refresh(asset)
        return asset

    def delete_asset(self, owner_id: UUID, asset_id: UUID) -> None:
        candidate = self.repo.get_asset(asset_id, owner_id)
        if candidate is None:
            raise ResourceNotFoundError
        project = self.repo.lock_project(candidate.project_id, owner_id)
        if project is None:
            raise ResourceNotFoundError
        asset = self.repo.lock_asset(asset_id, owner_id)
        if asset is None or asset.project_id != project.id:
            raise ResourceNotFoundError
        referenced_asset_ids = {
            node.get("data", {}).get("asset_id") for node in project.workflow.get("nodes", [])
        }
        if str(asset.id) in referenced_asset_ids:
            raise DomainValidationError("Remove the asset from the current workflow before deleting it")
        if project.cover_asset_id == asset.id:
            project.cover_asset_id = None
        asset.deleted_at = datetime.now(timezone.utc)
        asset.status = "deleting"
        if not self._asset_has_retained_references(project, asset.id):
            enqueue_asset_deletion(self.db, asset, reason="asset_deleted")
        self._commit()

    def _asset_has_retained_references(self, project: Project, asset_id: UUID) -> bool:
        expected = str(asset_id)
        for version in self.repo.list_versions(project.id):
            for node in version.snapshot.get("nodes", []):
                if str(node.get("data", {}).get("asset_id")) == expected:
                    return True
        for task in self.repo.list_project_generations(
            project.id, project.owner_id, latest_per_node=False
        ):
            references = task.parameters.get(INPUT_REFERENCES_PARAMETER, [])
            if not isinstance(references, list):
                continue
            if any(
                isinstance(reference, dict)
                and str(reference.get("asset_id")) == expected
                for reference in references
            ):
                return True
        return False

    def _generation_input_references(
        self,
        owner_id: UUID,
        project: Project,
        payload: GenerationCreate,
    ) -> list[dict]:
        if INPUT_REFERENCES_PARAMETER in payload.parameters:
            raise DomainValidationError(
                f"Model parameter is reserved for internal use: {INPUT_REFERENCES_PARAMETER}"
            )
        nodes = {
            str(node.get("id")): node
            for node in project.workflow.get("nodes", [])
            if node.get("id") is not None
        }
        target_id = str(payload.node_id)
        target_node = nodes.get(target_id)
        if target_node is None:
            raise DomainValidationError("Generation node does not exist in the current workflow")
        target_type = target_node.get("type")
        target_kind = "text" if target_type == "prompt" else target_type
        if target_kind != payload.kind:
            raise DomainValidationError(
                "Generation kind must match the target workflow node type"
            )
        direct_predecessors = [
            str(edge.get("source_node_id"))
            for edge in project.workflow.get("edges", [])
            if str(edge.get("target_node_id")) == target_id
        ]
        requested_input_ids = [str(input_node_id) for input_node_id in payload.input_node_ids]
        for input_id in requested_input_ids:
            if input_id == target_id:
                raise DomainValidationError(
                    "Generation input cannot reference the target node itself"
                )
            if input_id not in nodes:
                raise DomainValidationError(
                    "Generation input node does not exist in the current workflow"
                )
        if requested_input_ids != direct_predecessors:
            raise DomainValidationError(
                "Generation input_node_ids must exactly match every direct predecessor in workflow edge order"
            )
        if payload.kind in {"image", "video", "audio"} and not direct_predecessors:
            raise DomainValidationError(
                "Media generation target must have at least one direct predecessor"
            )
        direct_predecessor_kinds = [
            "text" if nodes[input_id].get("type") == "prompt" else nodes[input_id].get("type")
            for input_id in direct_predecessors
        ]
        if payload.kind == "image":
            if direct_predecessor_kinds.count("image") > 5:
                raise DomainValidationError(
                    "invalid-input: Image editing accepts at most five image predecessors"
                )
            if direct_predecessor_kinds.count("text") > 1:
                raise DomainValidationError(
                    "invalid-input: Image generation accepts at most one text predecessor"
                )
        allowed_input_kinds = WORKFLOW_INPUT_KINDS[payload.kind]
        references: list[dict] = []
        for ordinal, input_id in enumerate(direct_predecessors):
            node = nodes.get(input_id)
            node_kind = "text" if node.get("type") == "prompt" else node.get("type")
            if node_kind not in allowed_input_kinds:
                raise DomainValidationError(
                    f"Generation input type {node_kind} is not compatible with {payload.kind} generation"
                )
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            if node_kind == "text":
                references.append(
                    {
                        "node_id": input_id,
                        "kind": "text",
                        "prompt": str(data.get("prompt") or ""),
                        "ordinal": ordinal,
                    }
                )
                continue
            asset_id = data.get("asset_id")
            try:
                parsed_asset_id = UUID(str(asset_id))
            except (TypeError, ValueError):
                raise DomainValidationError(
                    "Media generation input must reference a ready project asset"
                ) from None
            asset = self.repo.get_asset(parsed_asset_id, owner_id)
            if asset is None or asset.project_id != project.id:
                raise DomainValidationError(
                    "Media generation input references an unavailable project asset"
                )
            if asset.kind != node_kind or asset.status != "ready":
                raise DomainValidationError(
                    "Media generation input must reference a ready project asset matching the node type"
                )
            references.append(
                {
                    "node_id": input_id,
                    "kind": node_kind,
                    "asset_id": str(asset.id),
                    "mime_type": asset.mime_type,
                    "byte_size": asset.byte_size,
                    "ordinal": ordinal,
                }
            )
        return references

    def create_generation(
        self, owner_id: UUID, payload: GenerationCreate, idempotency_key: str
    ) -> GenerationCreationResult:
        idempotency_key = idempotency_key.strip()
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or not idempotency_key.isascii()
            or any(character.isspace() for character in idempotency_key)
        ):
            raise DomainValidationError(
                "Idempotency-Key must contain 1 to 128 non-whitespace ASCII characters"
            )
        request_hash = generation_request_hash(payload)
        existing = self.repo.get_generation_by_idempotency(owner_id, idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError
            return GenerationCreationResult(existing, True)
        project = self.repo.lock_project(payload.project_id, owner_id)
        if project is None:
            raise ResourceNotFoundError
        if project.current_revision != payload.input_revision:
            raise RevisionConflictError(project.current_revision)
        node_ids = {str(node["id"]) for node in project.workflow.get("nodes", []) if "id" in node}
        if str(payload.node_id) not in node_ids:
            raise DomainValidationError("Generation node does not exist in the current workflow")
        input_references = self._generation_input_references(owner_id, project, payload)
        image_references = [
            reference for reference in input_references if reference.get("kind") == "image"
        ]
        text_references = [
            reference for reference in input_references if reference.get("kind") == "text"
        ]
        if payload.kind == "image":
            if len(image_references) > 5:
                raise DomainValidationError(
                    "invalid-input: Image editing accepts at most five image predecessors"
                )
            if len(text_references) > 1:
                raise DomainValidationError(
                    "invalid-input: Image generation accepts at most one text predecessor"
                )
            if not image_references and len(text_references) != 1:
                raise DomainValidationError(
                    "invalid-input: Image generation requires exactly one text predecessor"
                )
            total_input_image_bytes = sum(
                int(reference.get("byte_size") or 0) for reference in image_references
            )
            if total_input_image_bytes > settings.generation_input_images_max_total_bytes:
                raise DomainValidationError(
                    "invalid-input: Total predecessor image size exceeds the configured limit"
                )
        try:
            model_id = UUID(payload.model)
        except ValueError:
            raise DomainValidationError("Generation model must be a valid model ID") from None
        try:
            provider = ModelCatalogService(self.db).resolve_provider_configuration(
                owner_id, model_id, lock_model=True
            )
        except ResourceNotFoundError:
            raise DomainValidationError(
                "The selected model is unavailable or its API Key is not configured"
            ) from None
        if provider["kind"] != payload.kind:
            raise DomainValidationError("Generation kind must match the selected model")
        parameters = normalize_generation_parameters(provider, payload.parameters)
        validate_provider_input_support(provider, input_references)
        if image_references:
            multipart_scalar_parameters(parameters)
        effective_prompt = effective_generation_prompt(
            payload.kind, payload.prompt, input_references
        )
        validate_required_provider_prompt(provider, effective_prompt, input_references)
        task = GenerationTask(
            owner_id=owner_id,
            project_id=payload.project_id,
            node_id=payload.node_id,
            input_revision=payload.input_revision,
            kind=payload.kind,
            model=str(model_id),
            prompt=effective_prompt,
            parameters=stored_generation_parameters(parameters, input_references),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            max_attempts=settings.generation_max_attempts,
            result={},
        )
        self.db.add(task)
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            existing = self.repo.get_generation_by_idempotency(owner_id, idempotency_key)
            if existing is None:
                raise DomainValidationError("Data violates a project constraint") from error
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError from error
            return GenerationCreationResult(existing, True)
        self.db.refresh(task)
        return GenerationCreationResult(task, False)

    def create_image_project_generation(
        self,
        owner_id: UUID,
        payload: ImageProjectGenerationCreate,
        idempotency_key: str,
    ) -> ImageProjectGenerationCreationResult:
        idempotency_key = self._validate_upload_idempotency_key(idempotency_key)
        canonical = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self.repo.get_generation_by_idempotency(owner_id, idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError
            project = self.get_project(owner_id, existing.project_id)
            prompt_node_id = image_project_prompt_node_id(project, existing)
            return ImageProjectGenerationCreationResult(
                project, existing, prompt_node_id, existing.node_id, True
            )

        self._require_folder(owner_id, payload.folder_id, lock=True)
        try:
            provider = ModelCatalogService(self.db).resolve_provider_configuration(
                owner_id, payload.image_model_id, lock_model=True
            )
        except ResourceNotFoundError:
            raise DomainValidationError(
                "The selected image model is unavailable or its API Key is not configured"
            ) from None
        if provider["kind"] != "image":
            raise DomainValidationError("The selected model is not an image model")
        parameters = normalize_generation_parameters(provider, payload.parameters)

        project_id = uuid.uuid4()
        prompt_node_id = uuid.uuid4()
        image_node_id = uuid.uuid4()
        edge_id = uuid.uuid4()
        task_id = uuid.uuid4()
        input_references = [
            {
                "node_id": str(prompt_node_id),
                "kind": "text",
                "prompt": payload.prompt,
                "ordinal": 0,
            }
        ]
        validate_provider_input_support(provider, input_references)
        effective_prompt = effective_generation_prompt("image", "", input_references)
        validate_required_provider_prompt(provider, effective_prompt, input_references)
        workflow = Workflow.model_validate(
            {
                "schema_version": 1,
                "viewport": {"x": 0, "y": 0, "zoom": 1},
                "nodes": [
                    {
                        "id": prompt_node_id,
                        "type": "prompt",
                        "position": {"x": 80, "y": 120},
                        "size": {"width": 280, "height": 180},
                        "data": {
                            "title": "提示词节点1",
                            "prompt": payload.prompt,
                            "draft": "",
                            "model_id": None,
                            "parameters": {},
                            "asset_id": None,
                            "generation_task_id": None,
                            "messages": [],
                        },
                    },
                    {
                        "id": image_node_id,
                        "type": "image",
                        "position": {"x": 440, "y": 120},
                        "size": {"width": 320, "height": 320},
                        "data": {
                            "title": "图片节点1",
                            "prompt": "",
                            "draft": "",
                            "model_id": str(payload.image_model_id),
                            "parameters": parameters,
                            "asset_id": None,
                            "generation_task_id": task_id,
                            "messages": [],
                        },
                    },
                ],
                "edges": [
                    {
                        "id": edge_id,
                        "source_node_id": prompt_node_id,
                        "target_node_id": image_node_id,
                        "source_handle": "right",
                        "target_handle": "left",
                        "data": {},
                    }
                ],
            }
        )
        snapshot = _workflow_dict(workflow)
        project_name = payload.name or payload.prompt[:20].strip()
        if not project_name:
            project_name = datetime.now(timezone.utc).strftime("图片项目 %Y-%m-%d %H:%M")
        project = Project(
            id=project_id,
            owner_id=owner_id,
            folder_id=payload.folder_id,
            name=project_name,
            project_type="image",
            workflow=snapshot,
            workflow_schema_version=1,
            current_revision=1,
        )
        task = GenerationTask(
            id=task_id,
            owner_id=owner_id,
            project_id=project_id,
            node_id=image_node_id,
            input_revision=1,
            kind="image",
            model=str(payload.image_model_id),
            prompt=effective_prompt,
            parameters=stored_generation_parameters(parameters, input_references),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            max_attempts=settings.generation_max_attempts,
            result={},
        )
        try:
            # Keep the whole operation in one transaction, but flush the project
            # first so generation_tasks' direct and composite foreign keys can
            # be satisfied deterministically on PostgreSQL.
            self.db.add(project)
            self.db.flush()
            self.db.add_all(
                [
                    ProjectVersion(
                        project_id=project_id,
                        created_by_user_id=owner_id,
                        version_no=1,
                        schema_version=1,
                        snapshot=snapshot,
                        snapshot_hash=_snapshot_hash(snapshot),
                        reason="initial",
                    ),
                    task,
                ]
            )
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            existing = self.repo.get_generation_by_idempotency(owner_id, idempotency_key)
            if existing is None:
                raise DomainValidationError("Data violates a project constraint") from error
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError from error
            project = self.get_project(owner_id, existing.project_id)
            prompt_node_id = image_project_prompt_node_id(project, existing)
            return ImageProjectGenerationCreationResult(
                project, existing, prompt_node_id, existing.node_id, True
            )
        self.db.refresh(project)
        self.db.refresh(task)
        return ImageProjectGenerationCreationResult(
            project, task, prompt_node_id, image_node_id, False
        )

    def list_project_generations(
        self, owner_id: UUID, project_id: UUID, *, latest_per_node: bool
    ) -> list[GenerationTask]:
        self.get_project(owner_id, project_id)
        return self.repo.list_project_generations(
            project_id, owner_id, latest_per_node=latest_per_node
        )

    def get_generation(self, owner_id: UUID, task_id: UUID) -> GenerationTask:
        task = self.repo.get_generation(task_id, owner_id)
        if task is None:
            raise ResourceNotFoundError
        if self.repo.get_project(task.project_id, owner_id) is None:
            raise ResourceNotFoundError
        return task
