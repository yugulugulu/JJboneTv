from contextlib import contextmanager
from threading import Event, Thread
from uuid import UUID

from ..config import settings
from ..database import SessionLocal
from ..models import GenerationTask
from ..repositories.generation_tasks import GenerationTaskRepository


def claim_next_task(worker_id: str) -> GenerationTask | None:
    with SessionLocal.begin() as db:
        task = GenerationTaskRepository(db).claim_next(worker_id)
        if task is None:
            return None
        db.flush()
        db.refresh(task)
        db.expunge(task)
        return task


def claim_specific_task(task_id: UUID, worker_id: str) -> GenerationTask | None:
    with SessionLocal.begin() as db:
        task = GenerationTaskRepository(db).claim_specific(task_id, worker_id)
        if task is None:
            return None
        db.flush()
        db.refresh(task)
        db.expunge(task)
        return task


def heartbeat_task(task_id: UUID, lease_token: UUID) -> bool:
    with SessionLocal.begin() as db:
        return GenerationTaskRepository(db).heartbeat(task_id, lease_token)


def mark_provider_started(task_id: UUID, lease_token: UUID) -> bool:
    with SessionLocal.begin() as db:
        return GenerationTaskRepository(db).mark_provider_started(task_id, lease_token)


def fail_task(
    task_id: UUID, lease_token: UUID, message: str, *, outcome_unknown: bool = False
) -> bool:
    with SessionLocal.begin() as db:
        return GenerationTaskRepository(db).finish_failed(
            task_id, lease_token, message, outcome_unknown=outcome_unknown
        )


def retry_task(task_id: UUID, lease_token: UUID, message: str) -> bool:
    with SessionLocal.begin() as db:
        return GenerationTaskRepository(db).retry_or_fail(task_id, lease_token, message)


@contextmanager
def lease_heartbeat(task_id: UUID, lease_token: UUID):
    stopped = Event()

    def renew() -> None:
        while not stopped.wait(settings.generation_heartbeat_seconds):
            if not heartbeat_task(task_id, lease_token):
                stopped.set()
                return

    thread = Thread(target=renew, name=f"generation-heartbeat-{task_id}", daemon=True)
    thread.start()
    try:
        yield stopped
    finally:
        stopped.set()
        thread.join(timeout=1)
