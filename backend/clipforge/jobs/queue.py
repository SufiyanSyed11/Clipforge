"""Queue operations on the jobs table.

Claiming is a compare-and-set UPDATE (`... WHERE id = ? AND status = 'queued'`), so two
workers can never both run the same job, on SQLite or Postgres.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from clipforge.db import Job, JobEvent, session_scope, utcnow
from clipforge.qa import QAReport

MAX_ATTEMPTS = 3


def enqueue(kind: str, payload: dict[str, Any] | None = None, project_id: int | None = None) -> int:
    with session_scope() as s:
        job = Job(kind=kind, payload=payload or {}, project_id=project_id, status="queued")
        s.add(job)
        s.flush()
        s.add(JobEvent(job_id=job.id, message=f"Queued {kind} job"))
        return job.id


def claim_next(worker_id: str, kinds: list[str] | None = None) -> Job | None:
    with session_scope() as s:
        q = select(Job.id).where(Job.status == "queued").order_by(Job.id).limit(5)
        if kinds:
            q = q.where(Job.kind.in_(kinds))
        for job_id in s.scalars(q).all():
            now = utcnow()
            res = s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(
                    status="running",
                    worker_id=worker_id,
                    started_at=now,
                    heartbeat_at=now,
                    attempts=Job.attempts + 1,
                )
            )
            if res.rowcount == 1:
                s.add(JobEvent(job_id=job_id, message=f"Started by worker {worker_id}"))
                s.flush()
                return s.get(Job, job_id)
    return None


def log(job_id: int, message: str, level: str = "info", progress: float | None = None) -> None:
    with session_scope() as s:
        s.add(JobEvent(job_id=job_id, message=message, level=level, progress=progress))
        s.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=utcnow()))


def heartbeat(job_id: int) -> None:
    with session_scope() as s:
        s.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=utcnow()))


def complete(job_id: int, result: dict[str, Any], qa: list[QAReport]) -> None:
    with session_scope() as s:
        s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status="done",
                result=result,
                qa=[r.to_dict() for r in qa],
                finished_at=utcnow(),
            )
        )
        failed = [r.step for r in qa if not r.passed]
        msg = "Done" if not failed else f"Done with failing QA: {', '.join(failed)}"
        s.add(JobEvent(job_id=job_id, message=msg, level="info" if not failed else "warn"))


def fail(job_id: int, error: str, qa: list[QAReport] | None = None) -> None:
    with session_scope() as s:
        s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status="failed",
                error=error,
                qa=[r.to_dict() for r in qa] if qa else None,
                finished_at=utcnow(),
            )
        )
        s.add(JobEvent(job_id=job_id, message=f"Failed: {error}", level="error"))


def requeue_stale(stale_seconds: float) -> list[int]:
    """Re-queue running jobs whose worker stopped sending heartbeats (crash, closed window)."""
    cutoff = utcnow() - timedelta(seconds=stale_seconds)
    requeued: list[int] = []
    with session_scope() as s:
        stale = s.scalars(
            select(Job).where(Job.status == "running", Job.heartbeat_at < cutoff)
        ).all()
        for job in stale:
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "failed"
                job.error = f"Abandoned by worker {job.worker_id} {job.attempts} times"
                job.finished_at = utcnow()
                s.add(JobEvent(job_id=job.id, message=job.error, level="error"))
            else:
                job.status = "queued"
                s.add(
                    JobEvent(
                        job_id=job.id,
                        message=f"Worker {job.worker_id} stopped responding; re-queued",
                        level="warn",
                    )
                )
                requeued.append(job.id)
    return requeued
