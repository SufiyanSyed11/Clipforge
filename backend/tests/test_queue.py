"""P0-3: queue semantics."""

from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import update

from clipforge.db import Job, JobEvent, session_scope, utcnow
from clipforge.jobs import queue
from clipforge.jobs.worker import run_one


def _job(job_id: int) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)


def test_job_is_claimed_by_exactly_one_worker():
    ids = [queue.enqueue("ping", {"steps": 1, "delay": 0}) for _ in range(20)]
    claimed: list[tuple[str, int]] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while (job := queue.claim_next(name)) is not None:
            with lock:
                claimed.append((name, job.id))

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_ids = [j for _, j in claimed]
    assert sorted(claimed_ids) == ids  # every job claimed once, none twice


def test_ping_job_runs_to_done_with_logs_and_qa():
    job_id = queue.enqueue("ping", {"steps": 2, "delay": 0})
    assert run_one("test-worker") == job_id
    job = _job(job_id)
    assert job.status == "done"
    assert job.result == {"pong": True, "steps": 2}
    assert job.qa[0]["passed"] is True
    with session_scope() as s:
        messages = [e.message for e in s.query(JobEvent).filter_by(job_id=job_id)]
    assert "ping step 1/2" in messages and "ping step 2/2" in messages


def test_failing_task_ends_failed_with_error_logged():
    job_id = queue.enqueue("ping", {"fail": True})
    run_one("test-worker")
    job = _job(job_id)
    assert job.status == "failed"
    assert "ping asked to fail" in job.error


def test_unknown_kind_fails_cleanly():
    job_id = queue.enqueue("does-not-exist")
    run_one("test-worker")
    assert _job(job_id).status == "failed"


def test_abandoned_running_job_is_requeued_then_failed_after_max_attempts():
    job_id = queue.enqueue("ping", {"steps": 1, "delay": 0})
    old = utcnow() - timedelta(hours=1)
    for attempt in range(1, queue.MAX_ATTEMPTS + 1):
        assert queue.claim_next("dead-worker").id == job_id
        with session_scope() as s:
            s.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=old))
        requeued = queue.requeue_stale(stale_seconds=60)
        if attempt < queue.MAX_ATTEMPTS:
            assert requeued == [job_id]
            assert _job(job_id).status == "queued"
    job = _job(job_id)
    assert job.status == "failed"
    assert "Abandoned" in job.error


def test_empty_queue_returns_none():
    assert run_one("idle") is None
