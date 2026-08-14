import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.admin_schemas import AdminModelCreate, AdminModelGroupCreate, AdminModelUpdate
from app.config import Settings, settings
from app.runtime import RuntimeConfigurationError, validate_runtime_settings
from app.repositories.generation_tasks import GenerationTaskRepository
from app.services import generation_provider
from app.services.generation_provider import ProviderHTTPError
from app.worker import _fill_pool


def test_video_poll_schedule_uses_expected_bands_and_jitter(monkeypatch):
    monkeypatch.setattr(generation_provider.random, "uniform", lambda _start, _end: 1.25)
    submitted_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert generation_provider.video_poll_delay_seconds(
        submitted_at, submitted_at + timedelta(seconds=119)
    ) == 6.25
    assert generation_provider.video_poll_delay_seconds(
        submitted_at, submitted_at + timedelta(seconds=120)
    ) == 16.25
    assert generation_provider.video_poll_delay_seconds(
        submitted_at, submitted_at + timedelta(seconds=600)
    ) == 31.25


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"image_provider_concurrency": 21, "image_active_window": 20}, "IMAGE_PROVIDER"),
        ({"video_submit_concurrency": 11, "video_active_limit": 10}, "VIDEO_SUBMIT"),
        (
            {"generation_heartbeat_seconds": 30, "generation_lease_seconds": 30},
            "HEARTBEAT",
        ),
        ({"database_pool_size": 0}, "DATABASE_POOL_SIZE"),
        ({"database_max_overflow": -1}, "DATABASE_MAX_OVERFLOW"),
    ],
)
def test_worker_limits_fail_fast_even_in_development(overrides, message):
    config = Settings(_env_file=None, app_environment="development", **overrides)

    with pytest.raises(RuntimeConfigurationError, match=message):
        validate_runtime_settings("worker", config)


def test_runtime_limits_are_validated_for_groups_and_models():
    limits = {"max_active": 4, "max_submit_per_second": 3}
    group = AdminModelGroupCreate(name="provider", runtime_limits=limits)
    model = AdminModelCreate(
        group_id=uuid4(),
        model_name="image-v1",
        display_name="Image V1",
        kind="image",
        metadata={"family": "test", "runtime_limits": limits},
    )
    update = AdminModelUpdate(metadata={"runtime_limits": {"max_active": 2}})

    assert group.runtime_limits == limits
    assert model.metadata["family"] == "test"
    assert model.metadata["runtime_limits"] == limits
    assert update.metadata == {"runtime_limits": {"max_active": 2}}

    for invalid in (
        {"max_active": 0},
        {"max_active": True},
        {"unsupported": 1},
    ):
        with pytest.raises(ValidationError):
            AdminModelGroupCreate(name="provider", runtime_limits=invalid)
        with pytest.raises(ValidationError):
            AdminModelCreate(
                group_id=uuid4(),
                model_name="image-v1",
                display_name="Image V1",
                kind="image",
                metadata={"runtime_limits": invalid},
            )


def test_image_result_retry_is_bounded_and_releases_lease(monkeypatch):
    task = SimpleNamespace(
        poll_error_count=0,
        last_provider_error=None,
        error=None,
        status="running",
        execution_phase="image_result_processing",
        completed_at=None,
        temporary_result_path="/tmp/provider-response.json",
        next_attempt_at=None,
        lease_token=uuid4(),
        lease_owner="worker",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
    )

    class Transaction:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, *_args):
            return False

    class SessionFactory:
        @staticmethod
        def begin():
            return Transaction()

    monkeypatch.setattr(generation_provider, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        generation_provider,
        "GenerationTaskRepository",
        lambda _db: SimpleNamespace(leased_task=lambda *_args, **_kwargs: task),
    )
    monkeypatch.setattr(generation_provider.random, "uniform", lambda _start, _end: 0)
    monkeypatch.setattr(settings, "provider_retry_max_attempts", 3)

    assert generation_provider._retry_image_result(uuid4(), task.lease_token, "temporary") is False
    assert task.poll_error_count == 1
    assert task.status == "running"
    assert task.next_attempt_at is not None
    assert task.temporary_result_path == "/tmp/provider-response.json"
    assert task.lease_token is None

    task.poll_error_count = 2
    task.lease_token = uuid4()
    assert generation_provider._retry_image_result(uuid4(), task.lease_token, "final") is True
    assert task.poll_error_count == 3
    assert task.status == "failed"
    assert task.execution_phase == "failed"
    assert task.completed_at is not None
    assert task.temporary_result_path is None
    assert task.lease_token is None


def test_video_poll_http_error_reschedules_without_resubmitting_or_failing(monkeypatch):
    task_id = uuid4()
    lease_token = uuid4()
    rescheduled = []
    failed = []
    submitted = []

    monkeypatch.setattr(
        generation_provider,
        "_video_provider_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderHTTPError("provider unavailable", retryable=True)
        ),
    )
    monkeypatch.setattr(generation_provider, "_provider_error_backoff", lambda _count: 7)
    monkeypatch.setattr(
        generation_provider,
        "_reschedule_video_poll",
        lambda *args, **kwargs: rescheduled.append((args, kwargs)),
    )
    monkeypatch.setattr(
        generation_provider,
        "fail_task",
        lambda *args, **kwargs: failed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        generation_provider,
        "process_video_submission",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )

    generation_provider.process_video_poll(task_id, lease_token)

    assert len(rescheduled) == 1
    assert rescheduled[0][1]["delay_seconds"] == 7
    assert failed == []
    assert submitted == []


def test_fill_pool_never_claims_more_than_capacity():
    available = [SimpleNamespace(id=uuid4(), lease_token=uuid4()) for _ in range(5)]
    claimed = []
    processed = []

    def claim(_identity):
        if not available:
            return None
        task = available.pop(0)
        claimed.append(task.id)
        return task

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = set()
        assert _fill_pool(
            executor,
            futures,
            3,
            claim,
            lambda task_id, lease_token: processed.append((task_id, lease_token)),
            "test-worker",
        )
        for future in futures:
            future.result()

    assert len(claimed) == 3
    assert len(processed) == 3
    assert len(available) == 2


def test_channel_runtime_limits_count_image_and_video_together(monkeypatch):
    owner_id = uuid4()
    group_id = uuid4()
    task = SimpleNamespace(
        id=uuid4(),
        owner_id=owner_id,
        model=str(uuid4()),
        status="queued",
        execution_phase="queued",
        attempt_count=0,
        provider_request_started_at=None,
        provider_slot_reserved_at=None,
        lease_token=None,
        lease_owner=None,
        lease_expires_at=None,
        error=None,
    )
    model = SimpleNamespace(
        id=uuid4(),
        group_id=group_id,
        model_metadata={},
    )
    group = SimpleNamespace(
        runtime_limits={"max_active": 4, "max_submit_per_second": 4}
    )
    db = MagicMock()
    db.scalars.return_value = [task]
    db.get.return_value = group
    repo = GenerationTaskRepository(db)
    monkeypatch.setattr(repo, "_advisory_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repo, "_model", lambda _task: model)
    active_calls = []
    submit_calls = []

    def active_count(kind, **kwargs):
        active_calls.append((kind, kwargs))
        return 0

    def recent_submit_count(kind, _since, **kwargs):
        submit_calls.append((kind, kwargs))
        return 0

    monkeypatch.setattr(repo, "_active_count", active_count)
    monkeypatch.setattr(repo, "_recent_submit_count", recent_submit_count)

    claimed = repo._claim_new_active(
        "worker",
        kind="image",
        phase="image_requesting",
        global_limit=20,
        user_limit=4,
        now=datetime.now(timezone.utc),
    )

    assert claimed is task
    assert (None, {"group_id": group_id}) in active_calls
    assert (None, {"group_id": group_id}) in submit_calls


def test_spool_cleanup_keeps_recent_files_and_removes_stale_orphans(
    monkeypatch, tmp_path
):
    referenced = tmp_path / "referenced.json"
    stale_json = tmp_path / "stale.json"
    recent_part = tmp_path / "active.part"
    stale_part = tmp_path / "stale.part"
    for path in (referenced, stale_json, recent_part, stale_part):
        path.write_bytes(b"data")
    stale_timestamp = datetime.now(timezone.utc).timestamp() - 1000
    for path in (referenced, stale_json, stale_part):
        os.utime(path, (stale_timestamp, stale_timestamp))

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalars(self, _statement):
            return [str(referenced)]

    monkeypatch.setattr(generation_provider, "SessionLocal", Session)
    monkeypatch.setattr(generation_provider, "_image_spool_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "provider_read_timeout_seconds", 300)
    monkeypatch.setattr(settings, "generation_lease_seconds", 90)

    assert generation_provider.cleanup_orphaned_image_spool_files() == 2
    assert referenced.exists()
    assert recent_part.exists()
    assert not stale_json.exists()
    assert not stale_part.exists()


def test_generation_concurrency_migration_marks_unknown_legacy_video_tasks_failed():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260808_0010_generation_concurrency.py"
    )
    spec = importlib.util.spec_from_file_location("generation_concurrency_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = MagicMock()
    migration.op = operations

    migration.upgrade()

    executed_sql = "\n".join(str(call.args[0]) for call in operations.execute.call_args_list)
    assert "provider_result_unknown" in executed_sql
    assert "provider_outcome_unknown" in executed_sql
    assert "provider_task_id IS NULL" in executed_sql
