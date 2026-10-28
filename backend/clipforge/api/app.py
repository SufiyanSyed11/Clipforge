"""FastAPI application. Run with `clipforge-api` (or `uvicorn clipforge.api.app:app`)."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from clipforge import __version__
from clipforge.api.serializers import event_dict, job_dict
from clipforge.app_settings import AppConfig, load_config, save_config
from clipforge.core import disk
from clipforge.db import Clip, Job, JobEvent, Project, get_engine, session_scope
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


class CleanupRequest(BaseModel):
    target: Literal["intermediates", "broll_cache", "project"]
    project_id: int | None = None
    confirm: bool = False


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        get_engine()  # create tables before the first request
        yield

    app = FastAPI(title="ClipForge API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin, "http://127.0.0.1:3000", "http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"} | {
        h.strip().lower() for h in settings.allowed_hosts.split(",") if h.strip()
    }

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next):
        """The API is for the local web app only.
        - Host check: refuses DNS-rebinding requests aimed at 127.0.0.1.
        - Writes need the X-ClipForge header: browsers can't add it cross-site without a CORS
          preflight, which only the ClipForge web app passes, so other sites can't trigger
          uploads or jobs."""
        host = request.headers.get("host", "").rsplit(":", 1)[0].lower()
        if host.startswith("[") and "]" in request.headers.get("host", ""):
            host = request.headers["host"].split("]")[0] + "]"
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("x-clipforge") != "1"
        ):
            return JSONResponse({"detail": "Missing X-ClipForge header"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("clipforge.api").exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            {"detail": f"Internal error ({type(exc).__name__}); see the API window's log"},
            status_code=500,
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
        return {
            "text": text,
            "vision": vision,
            # Only whether keys are set; key values never leave the server.
            "stock": {
                "pexels": bool(settings.pexels_api_key),
                "pixabay": bool(settings.pixabay_api_key),
            },
        }

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
                    if job.status in ("done", "failed", "cancelled") and not events:
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

    BRANDING = {
        "logo": ({".png", ".jpg", ".jpeg", ".webp"}, 5 * 1024 * 1024, "logo_path"),
        "music": ({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}, 50 * 1024 * 1024, "bgm_path"),
        "sfx": ({".mp3", ".wav", ".m4a", ".ogg"}, 5 * 1024 * 1024, "sfx_path"),
    }

    @app.post("/api/settings/{kind}")
    def upload_branding(kind: str, file: UploadFile = File(...)) -> dict[str, Any]:
        """Upload the logo, the royalty-free music bed or the transition SFX."""
        if kind not in BRANDING:
            raise HTTPException(404, "Unknown upload")
        allowed, limit, field = BRANDING[kind]
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in allowed:
            raise HTTPException(422, f"Use one of: {', '.join(sorted(allowed))}")
        dest_dir = settings.data_dir / "branding"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{kind}{suffix}"
        tmp = dest.with_suffix(".part")
        size = 0
        with tmp.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, f"File too large (max {limit // 1024 // 1024} MB)")
                out.write(chunk)
        tmp.replace(dest)
        cfg = load_config()
        setattr(cfg, field, str(dest))
        return save_config(cfg).model_dump()

    @app.delete("/api/settings/{kind}")
    def remove_branding(kind: str) -> dict[str, Any]:
        if kind not in BRANDING:
            raise HTTPException(404, "Unknown upload")
        cfg = load_config()
        setattr(cfg, BRANDING[kind][2], None)
        return save_config(cfg).model_dump()

    @app.get("/api/storage")
    def storage_usage():
        st = get_settings()
        return disk.usage(st.data_dir, st.min_free_gb).to_dict()

    @app.post("/api/storage/cleanup")
    def storage_cleanup(body: CleanupRequest):
        st = get_settings()
        freed = 0
        if body.target == "intermediates":
            root = st.data_dir / "projects"
            dirs = sorted(root.glob("*")) if root.is_dir() else []
            if body.project_id is not None:
                dirs = [root / f"{body.project_id:05d}"]
            for pdir in dirs:
                if pdir.is_dir() and disk.safe_child(root, pdir):
                    freed += disk.clean_intermediates(pdir)
        elif body.target == "broll_cache":
            freed = disk.clean_broll_cache(st.data_dir)
        elif body.target == "project":
            if body.project_id is None or not body.confirm:
                raise HTTPException(422, "Deleting a project needs project_id and confirm=true")
            with session_scope() as s:
                proj = s.get(Project, body.project_id)
                if proj is None:
                    raise HTTPException(404, "Project not found")
                if any(
                    j.status in ("queued", "running")
                    for j in s.scalars(select(Job).where(Job.project_id == body.project_id))
                ):
                    raise HTTPException(409, "Cancel this project's running jobs first")
                s.execute(delete(Clip).where(Clip.project_id == body.project_id))
                # Keep the job history, detached from the deleted project.
                s.execute(
                    update(Job).where(Job.project_id == body.project_id).values(project_id=None)
                )
                s.delete(proj)
            pdir = st.data_dir / "projects" / f"{body.project_id:05d}"
            if pdir.is_dir() and disk.safe_child(st.data_dir / "projects", pdir):
                freed = disk._size(pdir)
                shutil.rmtree(pdir, ignore_errors=True)
        return {
            "freed_gb": round(freed / 1024**3, 3),
            **disk.usage(st.data_dir, st.min_free_gb).to_dict(),
        }

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int) -> dict[str, Any]:
        try:
            queue.request_cancel(job_id)
        except KeyError as e:
            raise HTTPException(404, "Job not found") from e
        with session_scope() as s:
            return job_dict(s.get(Job, job_id))

    @app.post("/api/jobs/{job_id}/retry", status_code=201)
    def retry_job(job_id: int) -> dict[str, Any]:
        try:
            new_id = queue.retry(job_id)
        except KeyError as e:
            raise HTTPException(404, "Job not found") from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        with session_scope() as s:
            return job_dict(s.get(Job, new_id))

    from clipforge.api.clips import router as clips_router
    from clipforge.api.learning import router as learning_router
    from clipforge.api.projects import router as projects_router

    app.include_router(projects_router)
    app.include_router(clips_router)
    app.include_router(learning_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("clipforge.api.app:app", host=s.api_host, port=s.api_port)


if __name__ == "__main__":
    main()
