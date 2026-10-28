"""FastAPI application. Run with `clipforge-api` (or `uvicorn clipforge.api.app:app`)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from clipforge import __version__
from clipforge.api.serializers import event_dict, job_dict
from clipforge.app_settings import AppConfig, load_config, save_config
from clipforge.db import Job, JobEvent, get_engine, session_scope
from clipforge.jobs import queue
from clipforge.jobs.registry import registered_kinds
from clipforge.settings import get_settings
from clipforge.tools.check_env import load_report

# Job kinds the generic /api/jobs endpoint may create. Pipeline jobs are created through
# the project endpoints so the rights gate can't be bypassed.
PUBLIC_JOB_KINDS = {"ping"}


class JobIn(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        get_engine()  # create tables before the first request
        yield

    app = FastAPI(title="ClipForge API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin, "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "job_kinds": registered_kinds()}

    @app.get("/api/env")
    def env_report() -> dict[str, Any]:
        report = load_report()
        if report is None:
            raise HTTPException(404, "No environment report yet. Run `clipforge-check`.")
        return report

    @app.get("/api/llm/status")
    def llm_status() -> dict[str, Any]:
        """Which model picks clips, and is it ready? Never includes keys."""
        from clipforge.llm import get_text_provider, get_vision_provider

        try:
            text = get_text_provider().check().to_dict()
        except ValueError as e:
            text = {"ok": False, "provider": settings.llm_provider, "detail": str(e)}
        vision = get_vision_provider().check().to_dict()
        return {"text": text, "vision": vision}

    @app.get("/api/settings")
    def get_app_settings() -> dict[str, Any]:
        return load_config().model_dump()

    @app.put("/api/settings")
    def put_app_settings(cfg: AppConfig) -> dict[str, Any]:
        try:
            return save_config(cfg).model_dump()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/jobs", status_code=201)
    def create_job(body: JobIn) -> dict[str, Any]:
        if body.kind not in PUBLIC_JOB_KINDS:
            raise HTTPException(422, f"Job kind '{body.kind}' can't be created directly")
        job_id = queue.enqueue(body.kind, body.payload)
        with session_scope() as s:
            return job_dict(s.get(Job, job_id))

    @app.get("/api/jobs")
    def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
        with session_scope() as s:
            jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()
            return [job_dict(j) for j in jobs]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if not job:
                raise HTTPException(404, "Job not found")
            return job_dict(job)

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: int, after: int = 0) -> StreamingResponse:
        """Server-Sent Events: every log line, then a final `end` event with the job."""
        with session_scope() as s:
            if not s.get(Job, job_id):
                raise HTTPException(404, "Job not found")

        def stream() -> Iterator[str]:
            last = after
            idle_ticks = 0
            while True:
                with session_scope() as s:
                    events = s.scalars(
                        select(JobEvent)
                        .where(JobEvent.job_id == job_id, JobEvent.id > last)
                        .order_by(JobEvent.id)
                    ).all()
                    job = s.get(Job, job_id)
                    for ev in events:
                        last = ev.id
                        yield f"id: {ev.id}\nevent: log\ndata: {json.dumps(event_dict(ev))}\n\n"
                    if job.status in ("done", "failed") and not events:
                        yield f"event: end\ndata: {json.dumps(job_dict(job))}\n\n"
                        return
                idle_ticks = 0 if events else idle_ticks + 1
                if idle_ticks and idle_ticks % 30 == 0:
                    yield ": keep-alive\n\n"
                time.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from clipforge.api.projects import router as projects_router

    app.include_router(projects_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("clipforge.api.app:app", host=s.api_host, port=s.api_port)


if __name__ == "__main__":
    main()
