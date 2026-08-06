from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenerationTask


class GenerationTaskRepository:
    def __init__(self, db: Session):
        self.db = db

    def recover_expired_leases(self, now: datetime) -> None:
        expired = list(
            self.db.scalars(
                select(GenerationTask)
                .where(
                    GenerationTask.status == "running",
                    GenerationTask.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for task in expired:
            if task.provider_request_started_at is not None:
                if task.kind == "video" and task.provider_task_id:
                    # The provider task is durable; resume polling after a worker
                    # restart instead of submitting a duplicate request.
                    task.status = "queued"
                    task.next_attempt_at = now
                    task.error = None
                else:
                    task.status = "failed"
                    task.error = "Worker 租约过期，供应商结果未知，请重新发起"
                    task.result = {**(task.result or {}), "provider_outcome_unknown": True}
                    task.completed_at = now
            else:
                task.status = "queued"
                task.next_attempt_at = now
            task.lease_token = None
            task.lease_owner = None
            task.lease_expires_at = None

    def claim_next(self, worker_id: str, now: datetime | None = None) -> GenerationTask | None:
        current = now or datetime.now(timezone.utc)
        self.recover_expired_leases(current)
        task = self.db.scalar(
            select(GenerationTask)
            .where(
                GenerationTask.status == "queued",
                GenerationTask.next_attempt_at <= current,
                GenerationTask.attempt_count < GenerationTask.max_attempts,
            )
            .order_by(GenerationTask.next_attempt_at, GenerationTask.created_at, GenerationTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return None
        task.status = "running"
        task.attempt_count += 1
        task.lease_token = uuid4()
        task.lease_owner = worker_id[:160]
        task.lease_expires_at = current + timedelta(seconds=settings.generation_lease_seconds)
        if not task.provider_task_id:
            task.provider_request_started_at = None
        task.error = None
        return task

    def claim_specific(
        self, task_id: UUID, worker_id: str, now: datetime | None = None
    ) -> GenerationTask | None:
        current = now or datetime.now(timezone.utc)
        task = self.db.scalar(
            select(GenerationTask)
            .where(
                GenerationTask.id == task_id,
                GenerationTask.status == "queued",
                GenerationTask.next_attempt_at <= current,
                GenerationTask.attempt_count < GenerationTask.max_attempts,
            )
            .with_for_update(skip_locked=True)
        )
        if task is None:
            return None
        task.status = "running"
        task.attempt_count += 1
        task.lease_token = uuid4()
        task.lease_owner = worker_id[:160]
        task.lease_expires_at = current + timedelta(seconds=settings.generation_lease_seconds)
        if not task.provider_task_id:
            task.provider_request_started_at = None
        task.error = None
        return task

    def heartbeat(self, task_id: UUID, lease_token: UUID, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task_id,
                GenerationTask.status == "running",
                GenerationTask.lease_token == lease_token,
            )
            .values(
                lease_expires_at=current + timedelta(seconds=settings.generation_lease_seconds),
                updated_at=current,
            )
        )
        return result.rowcount == 1

    def mark_provider_started(self, task_id: UUID, lease_token: UUID) -> bool:
        current = datetime.now(timezone.utc)
        result = self.db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task_id,
                GenerationTask.status == "running",
                GenerationTask.lease_token == lease_token,
            )
            .values(provider_request_started_at=current, updated_at=current)
        )
        return result.rowcount == 1

    def leased_task(self, task_id: UUID, lease_token: UUID, *, lock: bool = False):
        statement = select(GenerationTask).where(
            GenerationTask.id == task_id,
            GenerationTask.status == "running",
            GenerationTask.lease_token == lease_token,
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def finish_failed(
        self,
        task_id: UUID,
        lease_token: UUID,
        message: str,
        *,
        outcome_unknown: bool = False,
    ) -> bool:
        task = self.leased_task(task_id, lease_token, lock=True)
        if task is None:
            return False
        task.status = "failed"
        task.error = message.strip()[:2000] or "生成任务失败"
        task.result = {
            **(task.result or {}),
            **({"provider_outcome_unknown": True} if outcome_unknown else {}),
        }
        task.completed_at = datetime.now(timezone.utc)
        task.lease_token = None
        task.lease_owner = None
        task.lease_expires_at = None
        return True

    def retry_or_fail(self, task_id: UUID, lease_token: UUID, message: str) -> bool:
        task = self.leased_task(task_id, lease_token, lock=True)
        if task is None:
            return False
        now = datetime.now(timezone.utc)
        if task.attempt_count >= task.max_attempts:
            task.status = "failed"
            task.error = message.strip()[:2000] or "生成任务重试次数已用尽"
            task.completed_at = now
        else:
            task.status = "queued"
            task.error = message.strip()[:2000]
            task.next_attempt_at = now + timedelta(seconds=min(60, 2 ** task.attempt_count))
            task.provider_request_started_at = None
        task.lease_token = None
        task.lease_owner = None
        task.lease_expires_at = None
        return True
