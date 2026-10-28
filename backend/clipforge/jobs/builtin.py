"""Built-in jobs. `ping` is the Phase 0 end-to-end proof: API -> queue -> worker -> UI."""

from __future__ import annotations

import time

from clipforge.jobs.registry import JobContext, TaskError, TaskResult, task
from clipforge.qa import QAReport


@task("ping")
def ping(ctx: JobContext) -> TaskResult:
    steps = int(ctx.payload.get("steps", 3))
    delay = float(ctx.payload.get("delay", 0.5))
    if ctx.payload.get("fail"):
        raise TaskError("ping asked to fail")
    for i in range(1, steps + 1):
        time.sleep(delay)
        ctx.check_cancelled()
        ctx.log(f"ping step {i}/{steps}", progress=i / steps)
    qa = QAReport(step="ping")
    qa.check("worker received the job", True, f"job {ctx.job_id}")
    qa.check("all steps ran", True, f"{steps} steps")
    return TaskResult(result={"pong": True, "steps": steps}, qa=[qa])
