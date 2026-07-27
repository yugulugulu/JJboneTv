import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import Asset, StorageDeletionJob
from ..repositories.storage_deletion_jobs import StorageDeletionJobRepository
from .asset_storage import AssetStorageError, storage_for_asset


def deletion_idempotency_key(
    *, backend: str, bucket: str | None, environment: str, storage_key: str
) -> str:
    identity = f"{backend}\0{bucket or '-'}\0{environment}\0{storage_key}"
    return f"delete:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def enqueue_asset_deletion(
    db: Session,
    asset: Asset,
    *,
    reason: str,
) -> StorageDeletionJob | None:
    if asset.kind == "text" or not asset.storage_key:
        return None
    metadata = asset.asset_metadata if isinstance(asset.asset_metadata, dict) else {}
    backend = str(metadata.get("storage_backend") or "local")
    bucket = metadata.get("storage_bucket")
    environment = str(metadata.get("storage_environment") or settings.app_environment)
    key = deletion_idempotency_key(
        backend=backend,
        bucket=bucket,
        environment=environment,
        storage_key=asset.storage_key,
    )
    existing = db.scalar(
        select(StorageDeletionJob).where(StorageDeletionJob.idempotency_key == key)
    )
    if existing is not None:
        return existing
    job = StorageDeletionJob(
        asset_id=asset.id,
        owner_id=asset.owner_id,
        project_id=asset.project_id,
        storage_backend=backend,
        storage_bucket=bucket,
        storage_environment=environment,
        storage_key=asset.storage_key,
        reason=reason,
        idempotency_key=key,
    )
    db.add(job)
    return job


def enqueue_storage_snapshot_deletion(
    db: Session,
    *,
    owner_id: UUID,
    project_id: UUID,
    storage_key: str,
    metadata: dict,
    reason: str,
) -> StorageDeletionJob:
    backend = str(metadata.get("storage_backend") or "local")
    bucket = metadata.get("storage_bucket")
    environment = str(metadata.get("storage_environment") or settings.app_environment)
    key = deletion_idempotency_key(
        backend=backend,
        bucket=bucket,
        environment=environment,
        storage_key=storage_key,
    )
    existing = db.scalar(
        select(StorageDeletionJob).where(StorageDeletionJob.idempotency_key == key)
    )
    if existing is not None:
        return existing
    job = StorageDeletionJob(
        asset_id=None,
        owner_id=owner_id,
        project_id=project_id,
        storage_backend=backend,
        storage_bucket=bucket,
        storage_environment=environment,
        storage_key=storage_key,
        reason=reason,
        idempotency_key=key,
    )
    db.add(job)
    return job


def claim_next_storage_deletion(worker_id: str) -> StorageDeletionJob | None:
    with SessionLocal.begin() as db:
        job = StorageDeletionJobRepository(db).claim_next(worker_id)
        if job is None:
            return None
        db.flush()
        db.refresh(job)
        db.expunge(job)
        return job


def process_claimed_storage_deletion(job_id: UUID, lease_token: UUID) -> None:
    with SessionLocal() as db:
        job = StorageDeletionJobRepository(db).leased_job(job_id, lease_token)
        if job is None:
            return
        snapshot = {
            "storage_backend": job.storage_backend,
            "storage_bucket": job.storage_bucket,
            "storage_environment": job.storage_environment,
        }
        storage_key = job.storage_key

    try:
        storage_for_asset(snapshot).delete_object(storage_key)
    except (AssetStorageError, ValueError) as error:
        with SessionLocal.begin() as db:
            StorageDeletionJobRepository(db).retry_or_fail(job_id, lease_token, str(error))
        return

    with SessionLocal.begin() as db:
        StorageDeletionJobRepository(db).finish_succeeded(job_id, lease_token)
