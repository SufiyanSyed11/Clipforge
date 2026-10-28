"""Worker process: claims queued jobs and runs them. Start one or more with `clipforge-worker`."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
import traceback

from clipforge.jobs import queue
from clipforge.jobs.registry import JobContext, TaskError, get_task
from clipforge.settings import get_settings

log = logging.getLogger("clipforge.worker")


def _heartbeat_loop(job_id: int, stop: threading.Event, every: float) -> None:
    # Long steps (ffmpeg, whisper) don't log often; keep the job marked alive meanwhile.
    while not stop.wait(every):
        try:
            queue.heartbeat(job_id)
        except Exception:  # pragma: no cover - a missed heartbeat is not fatal
            log.exception("heartbeat failed")


def run_one(worker_id: str, kinds: list[str] | None = None) -> int | None:
    """Claim and run a single job. Returns its id, or None if the queue was empty."""
    settings = get_settings()
    queue.requeue_stale(settings.job_stale_seconds)
    job = queue.claim_next(worker_id, kinds)
    if job is None:
        return None
    ctx = JobContext(
        job_id=job.id, kind=job.kind, payload=job.payload or {}, project_id=job.project_id
    )
    fn = get_task(job.kind)
    if fn is None:
        queue.fail(job.id, f"No task registered for kind '{job.kind}'")
        return job.id

    stop = threading.Event()
    beat = threading.Thread(
        target=_heartbeat_loop,
        args=(job.id, stop, max(5.0, settings.job_stale_seconds / 5)),
        daemon=True,
    )
    beat.start()
    try:
        out = fn(ctx)
        queue.complete(job.id, out.result, out.qa)
    except TaskError as e:
        queue.fail(job.id, str(e), e.qa)
    except Exception as e:  # unexpected: keep the traceback in the job log
        queue.log(job.id, traceback.format_exc(), level="error")
        queue.fail(job.id, f"{type(e).__name__}: {e}")
    finally:
        stop.set()
    return job.id


def run_forever(worker_id: str, kinds: list[str] | None = None) -> None:
    poll = get_settings().worker_poll_seconds
    log.info("worker %s polling for jobs", worker_id)
    while True:
        if run_one(worker_id, kinds) is None:
            time.sleep(poll)


def main() -> None:
    parser = argparse.ArgumentParser(description="ClipForge job worker")
    parser.add_argument("--kinds", nargs="*", help="only run these job kinds")
    parser.add_argument("--once", action="store_true", help="run at most one job, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    if args.once:
        run_one(worker_id, args.kinds)
    else:
        run_forever(worker_id, args.kinds)


if __name__ == "__main__":
    main()
