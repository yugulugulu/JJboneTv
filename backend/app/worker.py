import argparse
import logging
import os
import socket
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Thread
from uuid import uuid4

from .config import settings
from .runtime import validate_runtime_settings
from .services.generation_provider import (
    cleanup_orphaned_image_spool_files,
    process_image_provider_request,
    process_image_result,
    process_video_poll,
    process_video_submission,
)
from .services.generation_tasks import (
    claim_image_request_task,
    claim_image_result_task,
    claim_video_poll_task,
    claim_video_submit_task,
)
from .services.storage_deletion_jobs import (
    claim_next_storage_deletion,
    process_claimed_storage_deletion,
)
from .services.trash_purge import purge_expired_trash


logger = logging.getLogger(__name__)


def worker_id(role: str) -> str:
    return f"{role}:{socket.gethostname()}:{os.getpid()}:{uuid4()}"


def _collect_completed(futures: set[Future]) -> None:
    for future in tuple(futures):
        if not future.done():
            continue
        futures.remove(future)
        try:
            future.result()
        except Exception:
            logger.exception("Worker pool job escaped its task-level error handler")


def _fill_pool(
    executor: ThreadPoolExecutor,
    futures: set[Future],
    capacity: int,
    claim,
    process,
    identity: str,
) -> bool:
    claimed_any = False
    while len(futures) < capacity:
        task = claim(identity)
        if task is None or task.lease_token is None:
            break
        futures.add(executor.submit(process, task.id, task.lease_token))
        claimed_any = True
    return claimed_any


def _maintenance_loop(stopped: Event, identity: str) -> None:
    next_trash_purge_at = 0.0
    next_spool_cleanup_at = 0.0
    while not stopped.is_set():
        now = time.monotonic()
        if now >= next_trash_purge_at:
            try:
                purge_expired_trash()
            except Exception:
                logger.exception("Unable to purge expired project trash")
            next_trash_purge_at = now + settings.trash_purge_interval_seconds
        if now >= next_spool_cleanup_at:
            try:
                cleanup_orphaned_image_spool_files()
            except Exception:
                logger.exception("Unable to clean orphaned image response files")
            next_spool_cleanup_at = now + max(60, settings.trash_purge_interval_seconds)
        try:
            deletion = claim_next_storage_deletion(identity)
            if deletion is not None and deletion.lease_token is not None:
                process_claimed_storage_deletion(deletion.id, deletion.lease_token)
                continue
        except Exception:
            logger.exception("Storage deletion maintenance failed")
        stopped.wait(settings.generation_worker_poll_seconds)


def run_image_worker(*, once: bool = False) -> None:
    identity = worker_id("image")
    stopped = Event()
    maintenance = Thread(
        target=_maintenance_loop,
        args=(stopped, f"{identity}:maintenance"),
        name="image-worker-maintenance",
        daemon=True,
    )
    maintenance.start()
    request_futures: set[Future] = set()
    result_futures: set[Future] = set()
    try:
        with ThreadPoolExecutor(
            max_workers=settings.image_provider_concurrency,
            thread_name_prefix="image-provider",
        ) as request_pool, ThreadPoolExecutor(
            max_workers=settings.image_result_concurrency,
            thread_name_prefix="image-result",
        ) as result_pool:
            while True:
                _collect_completed(request_futures)
                _collect_completed(result_futures)
                claimed = _fill_pool(
                    result_pool,
                    result_futures,
                    settings.image_result_concurrency,
                    claim_image_result_task,
                    process_image_result,
                    f"{identity}:result",
                )
                claimed = _fill_pool(
                    request_pool,
                    request_futures,
                    settings.image_provider_concurrency,
                    claim_image_request_task,
                    process_image_provider_request,
                    f"{identity}:request",
                ) or claimed
                if once:
                    if request_futures or result_futures:
                        for future in tuple(request_futures | result_futures):
                            future.result()
                    return
                if not claimed:
                    time.sleep(settings.generation_worker_poll_seconds)
    finally:
        stopped.set()
        maintenance.join(timeout=2)


def run_video_worker(*, once: bool = False) -> None:
    identity = worker_id("video")
    submit_futures: set[Future] = set()
    poll_futures: set[Future] = set()
    with ThreadPoolExecutor(
        max_workers=settings.video_submit_concurrency,
        thread_name_prefix="video-submit",
    ) as submit_pool, ThreadPoolExecutor(
        max_workers=settings.video_poll_concurrency,
        thread_name_prefix="video-poll",
    ) as poll_pool:
        while True:
            _collect_completed(submit_futures)
            _collect_completed(poll_futures)
            claimed = _fill_pool(
                poll_pool,
                poll_futures,
                settings.video_poll_concurrency,
                claim_video_poll_task,
                process_video_poll,
                f"{identity}:poll",
            )
            claimed = _fill_pool(
                submit_pool,
                submit_futures,
                settings.video_submit_concurrency,
                claim_video_submit_task,
                process_video_submission,
                f"{identity}:submit",
            ) or claimed
            if once:
                if submit_futures or poll_futures:
                    for future in tuple(submit_futures | poll_futures):
                        future.result()
                return
            if not claimed:
                time.sleep(settings.generation_worker_poll_seconds)


def run(*, role: str = "image", once: bool = False) -> None:
    validate_runtime_settings("worker")
    if role == "image":
        run_image_worker(once=once)
    elif role == "video":
        run_video_worker(once=once)
    else:
        raise ValueError(f"Unsupported worker role: {role}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JJboneTv generation worker")
    parser.add_argument("--role", choices=("image", "video"), required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run(role=args.role, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
