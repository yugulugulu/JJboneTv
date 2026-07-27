from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..models import (
    Asset,
    AssetEditRequest,
    Folder,
    GenerationOutput,
    GenerationTask,
    Project,
    ProjectVersion,
)


class ProjectDomainRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_folders(self, owner_id: UUID) -> list[Folder]:
        return list(
            self.db.scalars(
                select(Folder)
                .where(Folder.owner_id == owner_id, Folder.deleted_at.is_(None))
                .order_by(func.lower(Folder.name), Folder.id)
            )
        )

    def get_folder(self, folder_id: UUID, owner_id: UUID, *, deleted: bool = False) -> Folder | None:
        query = select(Folder).where(Folder.id == folder_id, Folder.owner_id == owner_id)
        query = query.where(Folder.deleted_at.is_not(None) if deleted else Folder.deleted_at.is_(None))
        return self.db.scalar(query)

    def lock_folder(self, folder_id: UUID, owner_id: UUID, *, deleted: bool = False) -> Folder | None:
        query = select(Folder).where(Folder.id == folder_id, Folder.owner_id == owner_id)
        query = query.where(Folder.deleted_at.is_not(None) if deleted else Folder.deleted_at.is_(None))
        return self.db.scalar(query.with_for_update())

    def list_projects(
        self,
        owner_id: UUID,
        folder_id: UUID | None = None,
        *,
        root_only: bool = False,
        limit: int = 100,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> list[Project]:
        query: Select = select(Project).where(
            Project.owner_id == owner_id, Project.deleted_at.is_(None)
        )
        if root_only:
            query = query.where(Project.folder_id.is_(None))
        elif folder_id is not None:
            query = query.where(Project.folder_id == folder_id)
        if cursor is not None:
            updated_at, project_id = cursor
            query = query.where(
                or_(
                    Project.updated_at < updated_at,
                    and_(Project.updated_at == updated_at, Project.id < project_id),
                )
            )
        return list(self.db.scalars(query.order_by(Project.updated_at.desc(), Project.id.desc()).limit(limit)))

    def first_ready_image_asset_ids(
        self, owner_id: UUID, project_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        if not project_ids:
            return {}

        ranked_assets = (
            select(
                Asset.project_id.label("project_id"),
                Asset.id.label("asset_id"),
                func.row_number()
                .over(
                    partition_by=Asset.project_id,
                    order_by=(Asset.created_at.asc(), Asset.id.asc()),
                )
                .label("position"),
            )
            .where(
                Asset.owner_id == owner_id,
                Asset.project_id.in_(project_ids),
                Asset.kind == "image",
                Asset.status == "ready",
                Asset.deleted_at.is_(None),
                Asset.storage_key.is_not(None),
                func.btrim(Asset.storage_key) != "",
            )
            .subquery()
        )
        rows = self.db.execute(
            select(ranked_assets.c.project_id, ranked_assets.c.asset_id).where(
                ranked_assets.c.position == 1
            )
        )
        return {project_id: asset_id for project_id, asset_id in rows}

    def lock_project(self, project_id: UUID, owner_id: UUID) -> Project | None:
        return self.db.scalar(
            select(Project)
            .where(
                Project.id == project_id,
                Project.owner_id == owner_id,
                Project.deleted_at.is_(None),
            )
            .with_for_update()
        )

    def get_project(self, project_id: UUID, owner_id: UUID, *, deleted: bool = False) -> Project | None:
        query = select(Project).where(Project.id == project_id, Project.owner_id == owner_id)
        query = query.where(Project.deleted_at.is_not(None) if deleted else Project.deleted_at.is_(None))
        return self.db.scalar(query)

    def save_workflow_atomic(
        self,
        project_id: UUID,
        owner_id: UUID,
        base_revision: int,
        workflow: dict,
        schema_version: int,
    ) -> tuple[int, datetime] | None:
        row = self.db.execute(
            update(Project)
            .where(
                Project.id == project_id,
                Project.owner_id == owner_id,
                Project.deleted_at.is_(None),
                Project.current_revision == base_revision,
            )
            .values(
                workflow=workflow,
                workflow_schema_version=schema_version,
                current_revision=Project.current_revision + 1,
                updated_at=func.now(),
            )
            .returning(Project.current_revision, Project.updated_at)
        ).one_or_none()
        return (row.current_revision, row.updated_at) if row else None

    def get_version(self, version_id: UUID, project_id: UUID) -> ProjectVersion | None:
        return self.db.scalar(
            select(ProjectVersion).where(
                ProjectVersion.id == version_id, ProjectVersion.project_id == project_id
            )
        )

    def list_versions(self, project_id: UUID) -> list[ProjectVersion]:
        return list(
            self.db.scalars(
                select(ProjectVersion)
                .where(ProjectVersion.project_id == project_id)
                .order_by(ProjectVersion.version_no.desc())
            )
        )

    def latest_version(self, project_id: UUID) -> ProjectVersion | None:
        return self.db.scalar(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project_id)
            .order_by(ProjectVersion.version_no.desc())
            .limit(1)
        )

    def prune_versions(self, project_id: UUID, cutoff: datetime, keep_latest: int = 100) -> None:
        protected = (
            select(ProjectVersion.id)
            .where(ProjectVersion.project_id == project_id)
            .order_by(ProjectVersion.version_no.desc())
            .limit(keep_latest)
        )
        self.db.execute(
            delete(ProjectVersion).where(
                ProjectVersion.project_id == project_id,
                ProjectVersion.created_at < cutoff,
                ProjectVersion.id.not_in(protected),
            )
        )

    def list_assets(self, project_id: UUID, owner_id: UUID, kind: str | None = None) -> list[Asset]:
        query = select(Asset).where(
            Asset.project_id == project_id, Asset.owner_id == owner_id, Asset.deleted_at.is_(None)
        )
        if kind is not None:
            query = query.where(Asset.kind == kind)
        return list(self.db.scalars(query.order_by(Asset.created_at.desc(), Asset.id)))

    def get_asset(self, asset_id: UUID, owner_id: UUID, *, include_deleted: bool = False) -> Asset | None:
        query = select(Asset).where(Asset.id == asset_id, Asset.owner_id == owner_id)
        if not include_deleted:
            query = query.where(Asset.deleted_at.is_(None))
        return self.db.scalar(query)

    def get_image_upload_by_idempotency(
        self, owner_id: UUID, project_id: UUID, idempotency_key_hash: str
    ) -> Asset | None:
        return self.db.scalar(
            select(Asset)
            .where(
                Asset.owner_id == owner_id,
                Asset.project_id == project_id,
                Asset.kind == "image",
                Asset.source == "upload",
                Asset.deleted_at.is_(None),
                Asset.asset_metadata["upload_idempotency_key_hash"].as_string()
                == idempotency_key_hash,
            )
            .order_by(Asset.created_at.asc(), Asset.id.asc())
            .limit(1)
        )

    def lock_asset(self, asset_id: UUID, owner_id: UUID) -> Asset | None:
        return self.db.scalar(
            select(Asset)
            .where(
                Asset.id == asset_id,
                Asset.owner_id == owner_id,
                Asset.deleted_at.is_(None),
            )
            .with_for_update()
        )

    def get_asset_edit_request(
        self, owner_id: UUID, idempotency_key: str
    ) -> AssetEditRequest | None:
        return self.db.scalar(
            select(AssetEditRequest).where(
                AssetEditRequest.owner_id == owner_id,
                AssetEditRequest.idempotency_key == idempotency_key,
            )
        )

    def list_all_project_assets(self, project_id: UUID, owner_id: UUID) -> list[Asset]:
        return list(
            self.db.scalars(
                select(Asset)
                .where(Asset.project_id == project_id, Asset.owner_id == owner_id)
                .order_by(Asset.created_at, Asset.id)
            )
        )

    def owned_project_assets(self, ids: set[UUID], owner_id: UUID, project_id: UUID) -> set[UUID]:
        if not ids:
            return set()
        return set(
            self.db.scalars(
                select(Asset.id).where(
                    Asset.id.in_(ids),
                    Asset.owner_id == owner_id,
                    Asset.project_id == project_id,
                    Asset.deleted_at.is_(None),
                )
            )
        )

    def project_assets_including_deleted(
        self, ids: set[UUID], owner_id: UUID, project_id: UUID
    ) -> list[Asset]:
        if not ids:
            return []
        return list(
            self.db.scalars(
                select(Asset).where(
                    Asset.id.in_(ids), Asset.owner_id == owner_id, Asset.project_id == project_id
                )
            )
        )

    def get_generation(self, task_id: UUID, owner_id: UUID) -> GenerationTask | None:
        return self.db.scalar(
            select(GenerationTask).where(
                GenerationTask.id == task_id, GenerationTask.owner_id == owner_id
            )
        )

    def get_generation_by_idempotency(
        self, owner_id: UUID, idempotency_key: str
    ) -> GenerationTask | None:
        return self.db.scalar(
            select(GenerationTask).where(
                GenerationTask.owner_id == owner_id,
                GenerationTask.idempotency_key == idempotency_key,
            )
        )

    def owned_project_generations(self, ids: set[UUID], owner_id: UUID, project_id: UUID) -> set[UUID]:
        if not ids:
            return set()
        return set(
            self.db.scalars(
                select(GenerationTask.id).where(
                    GenerationTask.id.in_(ids),
                    GenerationTask.owner_id == owner_id,
                    GenerationTask.project_id == project_id,
                )
            )
        )

    def generation_outputs(self, task_id: UUID) -> list[tuple[GenerationOutput, Asset]]:
        return list(
            self.db.execute(
                select(GenerationOutput, Asset)
                .join(Asset, Asset.id == GenerationOutput.asset_id)
                .where(GenerationOutput.generation_task_id == task_id)
                .order_by(GenerationOutput.ordinal)
            ).all()
        )

    def list_project_generations(
        self, project_id: UUID, owner_id: UUID, *, latest_per_node: bool
    ) -> list[GenerationTask]:
        tasks = list(
            self.db.scalars(
                select(GenerationTask)
                .where(
                    GenerationTask.project_id == project_id,
                    GenerationTask.owner_id == owner_id,
                )
                .order_by(
                    GenerationTask.created_at.desc(),
                    GenerationTask.id.desc(),
                )
            )
        )
        if not latest_per_node:
            return tasks
        seen: set[UUID] = set()
        latest: list[GenerationTask] = []
        for task in tasks:
            if task.node_id in seen:
                continue
            seen.add(task.node_id)
            latest.append(task)
        return latest
