import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AIModel, GenerationTask, ModelGroup


class GenerationTaskRepository:
    CLAIM_CANDIDATE_SCAN_LIMIT = 1024

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
            if task.execution_phase == "image_result_processing" and task.temporary_result_path:
                task.error = None
            elif task.execution_phase == "video_provider_processing" and task.provider_task_id:
                task.next_poll_at = now
                task.error = None
            elif task.provider_request_started_at is not None:
                task.status = "failed"
                task.execution_phase = "provider_result_unknown"
                task.error = "Worker 租约过期，供应商结果未知，请重新发起"
                task.result = {**(task.result or {}), "provider_outcome_unknown": True}
                task.completed_at = now
            else:
                task.status = "queued"
                task.execution_phase = "queued"
                task.next_attempt_at = now
                task.provider_slot_reserved_at = None
            task.lease_token = None
            task.lease_owner = None
            task.lease_expires_at = None

    def _advisory_lock(self, namespace: str, value: object) -> None:
        if self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return
        digest = hashlib.sha256(f"generation:{namespace}:{value}".encode()).digest()[:8]
        key = int.from_bytes(digest, byteorder="big", signed=True)
        self.db.execute(select(func.pg_advisory_xact_lock(key)))

    def _lease(self, task: GenerationTask, worker_id: str, current: datetime) -> GenerationTask:
        task.lease_token = uuid4()
        task.lease_owner = worker_id[:160]
        task.lease_expires_at = current + timedelta(seconds=settings.generation_lease_seconds)
        task.error = None
        return task

    def _active_count(
        self,
        kind: str | None,
        *,
        owner_id=None,
        model: str | None = None,
        group_id: UUID | None = None,
    ) -> int:
        statement = select(func.count()).select_from(GenerationTask).where(
            GenerationTask.status == "running",
        )
        if kind is not None:
            statement = statement.where(GenerationTask.kind == kind)
        if owner_id is not None:
            statement = statement.where(GenerationTask.owner_id == owner_id)
        if model is not None:
            statement = statement.where(GenerationTask.model == model)
        if group_id is not None:
            model_ids = [
                str(value)
                for value in self.db.scalars(select(AIModel.id).where(AIModel.group_id == group_id))
            ]
            if not model_ids:
                return 0
            statement = statement.where(GenerationTask.model.in_(model_ids))
        return int(self.db.scalar(statement) or 0)

    def _recent_submit_count(
        self,
        kind: str | None,
        since: datetime,
        *,
        model: str | None = None,
        group_id: UUID | None = None,
    ) -> int:
        reserved_or_started = func.coalesce(
            GenerationTask.provider_request_started_at,
            GenerationTask.provider_slot_reserved_at,
        )
        statement = select(func.count()).select_from(GenerationTask).where(
            reserved_or_started >= since,
        )
        if kind is not None:
            statement = statement.where(GenerationTask.kind == kind)
        if model is not None:
            statement = statement.where(GenerationTask.model == model)
        if group_id is not None:
            model_ids = [
                str(value)
                for value in self.db.scalars(select(AIModel.id).where(AIModel.group_id == group_id))
            ]
            if not model_ids:
                return 0
            statement = statement.where(GenerationTask.model.in_(model_ids))
        return int(self.db.scalar(statement) or 0)

    def _model(self, task: GenerationTask) -> AIModel | None:
        try:
            model_id = UUID(task.model)
        except (TypeError, ValueError):
            return None
        return self.db.get(AIModel, model_id)

    @staticmethod
    def _model_runtime_limits(model: AIModel | None) -> dict:
        metadata = model.model_metadata if model and isinstance(model.model_metadata, dict) else {}
        limits = metadata.get("runtime_limits")
        return limits if isinstance(limits, dict) else {}

    @staticmethod
    def _group_runtime_limits(group: ModelGroup | None) -> dict:
        return group.runtime_limits if group and isinstance(group.runtime_limits, dict) else {}

    @staticmethod
    def _positive_int(limits: dict, key: str) -> int | None:
        value = limits.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def _claim_new_active(
        self,
        worker_id: str,
        *,
        kind: str,
        phase: str,
        global_limit: int,
        user_limit: int,
        now: datetime,
    ) -> GenerationTask | None:
        # The media-wide lock serializes the limit check and state transition across
        # every worker container. The remaining locks preserve the documented order
        # and make later channel/model-specific limits safe during horizontal scale.
        self._advisory_lock("media", kind)
        if self._active_count(kind) >= global_limit:
            return None
        candidates = list(
            self.db.scalars(
                select(GenerationTask)
                .where(
                    GenerationTask.kind == kind,
                    GenerationTask.status == "queued",
                    GenerationTask.execution_phase == "queued",
                    GenerationTask.next_attempt_at <= now,
                    GenerationTask.attempt_count < GenerationTask.max_attempts,
                )
                .order_by(GenerationTask.next_attempt_at, GenerationTask.created_at, GenerationTask.id)
                .with_for_update(skip_locked=True)
                .limit(self.CLAIM_CANDIDATE_SCAN_LIMIT)
            )
        )
        models = {task.id: self._model(task) for task in candidates}
        groups = {
            model.group_id: self.db.get(ModelGroup, model.group_id)
            for model in models.values()
            if model is not None
        }
        # Acquire every scoped lock in the same category/value order. This avoids
        # deadlocks when image and video models share a channel.
        for group_id in sorted({str(model.group_id) for model in models.values() if model}):
            self._advisory_lock("channel", group_id)
        for model_id in sorted({task.model for task in candidates}):
            self._advisory_lock("model", model_id)
        for owner_id in sorted({str(task.owner_id) for task in candidates}):
            self._advisory_lock("user", owner_id)

        for task in candidates:
            model = models[task.id]
            group = groups.get(model.group_id) if model is not None else None
            if self._active_count(kind, owner_id=task.owner_id) >= user_limit:
                continue
            group_limits = self._group_runtime_limits(group)
            model_limits = self._model_runtime_limits(model)
            group_active_limit = self._positive_int(group_limits, "max_active")
            model_active_limit = self._positive_int(model_limits, "max_active")
            if group_active_limit is not None and model is not None:
                if self._active_count(None, group_id=model.group_id) >= group_active_limit:
                    continue
            if model_active_limit is not None:
                if self._active_count(kind, model=task.model) >= model_active_limit:
                    continue
            since = now - timedelta(seconds=1)
            group_rate_limit = self._positive_int(group_limits, "max_submit_per_second")
            model_rate_limit = self._positive_int(model_limits, "max_submit_per_second")
            if group_rate_limit is not None and model is not None:
                if self._recent_submit_count(None, since, group_id=model.group_id) >= group_rate_limit:
                    continue
            if model_rate_limit is not None:
                if self._recent_submit_count(kind, since, model=task.model) >= model_rate_limit:
                    continue
            task.status = "running"
            task.execution_phase = phase
            task.attempt_count += 1
            task.provider_request_started_at = None
            task.provider_slot_reserved_at = now
            return self._lease(task, worker_id, now)
        return None

    def claim_image_request(self, worker_id: str, now: datetime | None = None):
        current = now or datetime.now(timezone.utc)
        self.recover_expired_leases(current)
        return self._claim_new_active(
            worker_id,
            kind="image",
            phase="image_requesting",
            global_limit=settings.image_active_window,
            user_limit=settings.user_image_active_limit,
            now=current,
        )

    def claim_image_result(self, worker_id: str, now: datetime | None = None):
        current = now or datetime.now(timezone.utc)
        self.recover_expired_leases(current)
        task = self.db.scalar(
            select(GenerationTask)
            .where(
                GenerationTask.kind == "image",
                GenerationTask.status == "running",
                GenerationTask.execution_phase == "image_result_processing",
                GenerationTask.temporary_result_path.is_not(None),
                GenerationTask.next_attempt_at <= current,
                GenerationTask.lease_token.is_(None),
            )
            .order_by(GenerationTask.next_attempt_at, GenerationTask.created_at, GenerationTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return self._lease(task, worker_id, current) if task is not None else None

    def claim_video_submit(self, worker_id: str, now: datetime | None = None):
        current = now or datetime.now(timezone.utc)
        self.recover_expired_leases(current)
        return self._claim_new_active(
            worker_id,
            kind="video",
            phase="video_submitting",
            global_limit=settings.video_active_limit,
            user_limit=settings.user_video_active_limit,
            now=current,
        )

    def claim_video_poll(self, worker_id: str, now: datetime | None = None):
        current = now or datetime.now(timezone.utc)
        self.recover_expired_leases(current)
        task = self.db.scalar(
            select(GenerationTask)
            .where(
                GenerationTask.kind == "video",
                GenerationTask.status == "running",
                GenerationTask.execution_phase == "video_provider_processing",
                GenerationTask.provider_task_id.is_not(None),
                GenerationTask.next_poll_at <= current,
                GenerationTask.lease_token.is_(None),
            )
            .order_by(GenerationTask.next_poll_at, GenerationTask.created_at, GenerationTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return self._lease(task, worker_id, current) if task is not None else None

    def claim_next(self, worker_id: str, now: datetime | None = None) -> GenerationTask | None:
        return self.claim_image_request(worker_id, now) or self.claim_video_submit(worker_id, now)

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
        task.execution_phase = "video_submitting" if task.kind == "video" else "image_requesting"
        task.attempt_count += 1
        task.lease_token = uuid4()
        task.lease_owner = worker_id[:160]
        task.lease_expires_at = current + timedelta(seconds=settings.generation_lease_seconds)
        if not task.provider_task_id:
            task.provider_request_started_at = None
            task.provider_slot_reserved_at = current
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
        task.execution_phase = "provider_result_unknown" if outcome_unknown else "failed"
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
            task.execution_phase = "failed"
            task.error = message.strip()[:2000] or "生成任务重试次数已用尽"
            task.completed_at = now
        else:
            task.status = "queued"
            task.execution_phase = "queued"
            task.error = message.strip()[:2000]
            delay = min(settings.provider_retry_max_delay_seconds, 2 ** task.attempt_count)
            task.next_attempt_at = now + timedelta(seconds=delay)
            task.provider_request_started_at = None
            task.provider_slot_reserved_at = None
        task.lease_token = None
        task.lease_owner = None
        task.lease_expires_at = None
        return True
