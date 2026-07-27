from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import StorageDeletionJob


class StorageDeletionJobRepository:
    def __init__(self, db: Session):
        self.db = db

    def recover_expired_leases(self, now: datetime) -> None:
        expired = list(
            self.db.scalars(
                select(StorageDeletionJob)
                .where(
                    StorageDeletionJob.status == "running",
                    StorageDeletionJob.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for job in expired:
            if job.attempt_count >= job.max_attempts:
                job.status = "failed"
                job.completed_at = now
                job.last_error = job.last_error or "Storage deletion lease expired"
            else:
                job.status = "queued"
                job.next_attempt_at = now
            job.lease_token = None
            job.lease_owner = None
            job.lease_expires_at = None

    def claim_next(
        self, worker_id: str, now: datetime | None = None
    ) -> StorageDeletionJob | None:
        current = now or datetime.now(timezone.utc)
        self.recover_expired_leases(current)
        job = self.db.scalar(
            select(StorageDeletionJob)
            .where(
                StorageDeletionJob.status == "queued",
                StorageDeletionJob.next_attempt_at <= current,
                StorageDeletionJob.attempt_count < StorageDeletionJob.max_attempts,
            )
            .order_by(
                StorageDeletionJob.next_attempt_at,
                StorageDeletionJob.created_at,
                StorageDeletionJob.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = "running"
        job.attempt_count += 1
        job.lease_token = uuid4()
        job.lease_owner = worker_id[:160]
        job.lease_expires_at = current + timedelta(seconds=settings.generation_lease_seconds)
        job.last_error = None
        return job

    def leased_job(
        self, job_id: UUID, lease_token: UUID, *, lock: bool = False
    ) -> StorageDeletionJob | None:
        statement = select(StorageDeletionJob).where(
            StorageDeletionJob.id == job_id,
            StorageDeletionJob.status == "running",
            StorageDeletionJob.lease_token == lease_token,
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def finish_succeeded(self, job_id: UUID, lease_token: UUID) -> bool:
        job = self.leased_job(job_id, lease_token, lock=True)
        if job is None:
            return False
        now = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.completed_at = now
        job.lease_token = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error = None
        return True

    def retry_or_fail(self, job_id: UUID, lease_token: UUID, message: str) -> bool:
        job = self.leased_job(job_id, lease_token, lock=True)
        if job is None:
            return False
        now = datetime.now(timezone.utc)
        job.last_error = (message.strip() or "Storage deletion failed")[:2000]
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
            job.completed_at = now
        else:
            job.status = "queued"
            job.next_attempt_at = now + timedelta(seconds=min(3600, 2 ** job.attempt_count))
        job.lease_token = None
        job.lease_owner = None
        job.lease_expires_at = None
        return True
