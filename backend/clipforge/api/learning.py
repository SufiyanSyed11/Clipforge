"""Learning loop endpoints: YouTube connection, refresh, CSV import, insights."""

from __future__ import annotations

import html
import time
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from clipforge.core import learn, youtube
from clipforge.db import Job, session_scope
from clipforge.jobs import queue
from clipforge.jobs.registry import JobContext, TaskError, TaskResult, task
from clipforge.qa import QAReport
from clipforge.settings import get_settings

router = APIRouter(prefix="/api/learning", tags=["learning"])
_PENDING: dict[str, dict] = {}  # OAuth state -> PKCE verifier (10 minutes)


def oauth() -> youtube.OAuth:
    s = get_settings()
    if not (s.youtube_client_id and s.youtube_client_secret):
        raise HTTPException(
            422,
            "Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET (Google Cloud OAuth client, "
            "type Desktop app) to .env, or import a CSV instead.",
        )
    redirect = f"http://{s.api_host}:{s.api_port}/api/learning/youtube/callback"
    return youtube.OAuth(s.youtube_client_id, s.youtube_client_secret, redirect)


@router.get("")
def get_learning() -> dict[str, Any]:
    s = get_settings()
    return {
        "insights": learn.insights_dict(learn.load_insights()),
        "youtube": youtube.status(s.data_dir),
        "youtube_configured": bool(s.youtube_client_id and s.youtube_client_secret),
        "days": s.learning_days,
        "prompt_context": learn.learning_context(),
    }


class ConnectIn(BaseModel):
    allow_upload: bool = False


@router.post("/youtube/connect")
def connect(body: ConnectIn) -> dict[str, str]:
    url, pending = oauth().start(body.allow_upload)
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["created"] > 600]:
        _PENDING.pop(k, None)
    _PENDING[pending["state"]] = pending
    return {"auth_url": url}


@router.get("/youtube/callback", response_class=HTMLResponse)
def callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    pending = _PENDING.pop(state, None)
    web = html.escape(get_settings().web_origin, quote=True)
    if error or not code or pending is None:
        # Query parameters are attacker-controlled: escape before putting them in HTML.
        msg = html.escape(error or "sign-in expired or was not started here")
        return HTMLResponse(
            f"<p>YouTube connection failed: {msg}.</p><a href='{web}/learning'>Back</a>", 400
        )
    try:
        tok = oauth().finish(code, pending["verifier"])
    except youtube.YouTubeError as e:
        return HTMLResponse(f"<p>{html.escape(str(e))}</p><a href='{web}/learning'>Back</a>", 400)
    youtube.save_token(get_settings().data_dir, tok)
    return HTMLResponse(f"<meta http-equiv='refresh' content='0;url={web}/learning?connected=1'>")


@router.post("/youtube/disconnect")
def disconnect() -> dict[str, Any]:
    youtube.forget(get_settings().data_dir)
    return get_learning()


class RefreshIn(BaseModel):
    days: int = Field(default=3, ge=1, le=30)


@router.post("/refresh")
def refresh(body: RefreshIn) -> dict[str, Any]:
    if not youtube.status(get_settings().data_dir)["connected"]:
        raise HTTPException(422, "Connect YouTube first, or import a CSV")
    job_id = queue.enqueue("refresh_learning", {"days": body.days})
    with session_scope() as s:
        from clipforge.api.serializers import job_dict

        return job_dict(s.get(Job, job_id))


@task("refresh_learning")
def refresh_learning(ctx: JobContext) -> TaskResult:
    s = get_settings()
    days = int(ctx.payload.get("days", s.learning_days))
    yt = youtube.YouTube(oauth(), s.data_dir)
    ctx.log(f"Reading the last {days} days of uploads")
    try:
        videos = yt.recent_uploads(days)
    except youtube.YouTubeError as e:
        raise TaskError(str(e)) from e
    ins = learn.analyse(videos, "YouTube API", days)
    learn.save_insights(ins)
    qa = QAReport(step="learning")
    qa.check("uploads analysed", ins.videos >= 2, ins.summary, severity="warn")
    return TaskResult(result={"videos": ins.videos}, qa=[qa])


@router.post("/csv")
def import_csv(file: UploadFile = File(...), days: int = 3) -> dict[str, Any]:
    raw = file.file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "CSV too large (max 10 MB)")
    try:
        videos = learn.parse_csv(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    ins = learn.analyse(learn.recent(videos, days), "CSV import", days)
    learn.save_insights(ins)
    return get_learning()
