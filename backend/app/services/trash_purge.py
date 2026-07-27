from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select

from ..config import settings
from ..database import SessionLocal
from ..models import Asset, Folder, Project
from .storage_deletion_jobs import enqueue_asset_deletion


PURGE_BATCH_SIZE = 20


def purge_expired_trash(*, now: datetime | None = None) -> tuple[int, int]:
    """Purge expired trash rows and durably queue their objects for deletion."""

    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=settings.trash_retention_days)
    purged_projects = 0
    purged_folders = 0
    with SessionLocal.begin() as db:
        projects = list(
            db.scalars(
                select(Project)
                .where(Project.deleted_at.is_not(None), Project.deleted_at <= cutoff)
                .order_by(Project.deleted_at, Project.id)
                .with_for_update(skip_locked=True)
                .limit(PURGE_BATCH_SIZE)
            )
        )
        for project in projects:
            assets = list(
                db.scalars(
                    select(Asset).where(
                        Asset.project_id == project.id,
                        Asset.owner_id == project.owner_id,
                    )
                )
            )
            for asset in assets:
                enqueue_asset_deletion(db, asset, reason="project_purged")
            db.delete(project)
            purged_projects += 1

        folders = list(
            db.scalars(
                select(Folder)
                .where(
                    Folder.deleted_at.is_not(None),
                    Folder.deleted_at <= cutoff,
                    ~exists(select(Project.id).where(Project.folder_id == Folder.id)),
                )
                .order_by(Folder.deleted_at, Folder.id)
                .with_for_update(skip_locked=True)
                .limit(PURGE_BATCH_SIZE)
            )
        )
        for folder in folders:
            db.delete(folder)
            purged_folders += 1
    return purged_projects, purged_folders
