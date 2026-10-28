"""Task registry. A task is a function `(ctx) -> TaskResult` registered under a job kind."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clipforge.jobs import queue
from clipforge.qa import QAReport


class TaskCancelled(Exception):
    """Raised inside a task when the owner cancelled the job."""


class TaskError(Exception):
    """A task failure that should be shown to the user as-is (no traceback)."""

    def __init__(self, message: str, qa: list[QAReport] | None = None) -> None:
        super().__init__(message)
        self.qa = qa or []


@dataclass
class TaskResult:
    result: dict[str, Any] = field(default_factory=dict)
    qa: list[QAReport] = field(default_factory=list)


@dataclass
class JobContext:
    job_id: int
    kind: str
    payload: dict[str, Any]
    project_id: int | None

    def log(self, message: str, progress: float | None = None, level: str = "info") -> None:
        queue.log(self.job_id, message, level=level, progress=progress)

    def heartbeat(self) -> None:
        queue.heartbeat(self.job_id)

    def cancelled(self) -> bool:
        return queue.is_cancel_requested(self.job_id)

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise TaskCancelled()


TaskFn = Callable[[JobContext], TaskResult]
_TASKS: dict[str, TaskFn] = {}


def task(kind: str) -> Callable[[TaskFn], TaskFn]:
    def register(fn: TaskFn) -> TaskFn:
        _TASKS[kind] = fn
        return fn

    return register


def get_task(kind: str) -> TaskFn | None:
    load_builtin_tasks()
    return _TASKS.get(kind)


def registered_kinds() -> list[str]:
    load_builtin_tasks()
    return sorted(_TASKS)


def load_builtin_tasks() -> None:
    # Importing registers the tasks.
    from clipforge.api import learning  # noqa: F401  (refresh_learning task)
    from clipforge.jobs import builtin  # noqa: F401
    from clipforge.pipeline import clip_tasks, tasks  # noqa: F401
