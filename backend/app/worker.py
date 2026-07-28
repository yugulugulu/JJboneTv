import argparse
import logging
import os
import socket
import time
from uuid import uuid4

from .config import settings
from .runtime import validate_runtime_settings
from .services.generation_provider import process_claimed_generation_task
from .services.generation_tasks import claim_next_task
from .services.storage_deletion_jobs import (
    claim_next_storage_deletion,
    process_claimed_storage_deletion,
)
from .services.trash_purge import purge_expired_trash


logger = logging.getLogger(__name__)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"


def run(*, once: bool = False) -> None:
    validate_runtime_settings("worker")
    identity = worker_id()
    prefer_deletion = True
    next_trash_purge_at = 0.0
    while True:
        monotonic_now = time.monotonic()
        if monotonic_now >= next_trash_purge_at:
            try:
                purge_expired_trash()
            except Exception:
                logger.exception("Unable to purge expired project trash")
            next_trash_purge_at = monotonic_now + settings.trash_purge_interval_seconds
        deletion = claim_next_storage_deletion(identity) if prefer_deletion else None
        task = None if deletion is not None else claim_next_task(identity)
        if task is None and deletion is None and not prefer_deletion:
            deletion = claim_next_storage_deletion(identity)
        prefer_deletion = not prefer_deletion
        if task is None and deletion is None:
            if once:
                return
            time.sleep(settings.generation_worker_poll_seconds)
            continue
        if deletion is not None:
            process_claimed_storage_deletion(deletion.id, deletion.lease_token)
        else:
            process_claimed_generation_task(task.id, task.lease_token)
        if once:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JJboneTv generation worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
