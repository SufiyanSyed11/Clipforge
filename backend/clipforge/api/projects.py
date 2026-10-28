"""Project endpoints. Creating a project enforces the rights gate before anything is ingested."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from clipforge.api.serializers import clip_dict, job_dict, project_dict
from clipforge.core.ingest import IngestError, validate_url
from clipforge.db import Clip, Job, Project, session_scope, utcnow
from clipforge.jobs import queue
from clipforge.storage import get_storage

router = APIRouter(prefix="/api/projects", tags=["projects"])

MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac"}


@router.post("", status_code=201)
def create_project(
    rights_confirmed: bool = Form(False),
    name: str = Form(""),
    url: str = Form(""),
    clip_count: int = Form(8),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    if not rights_confirmed:
        raise HTTPException(
            422, "Confirm you have the rights or permission to clip this content first."
        )
    if bool(url.strip()) == bool(file and file.filename):
        raise HTTPException(422, "Give either a YouTube/media URL or a file, not both.")
    if not 1 <= clip_count <= 20:
        raise HTTPException(422, "clip_count must be between 1 and 20")
    if url.strip():
        try:
            url = validate_url(url)
        except IngestError as e:
            raise HTTPException(422, str(e)) from e
    elif Path(file.filename).suffix.lower() not in MEDIA_SUFFIXES:
        raise HTTPException(422, f"Unsupported file type: {Path(file.filename).suffix}")

    with session_scope() as s:
        p = Project(
            name=name.strip() or (url or file.filename)[:200],
            source_type="url" if url else "upload",
            source=url or "",
            rights_confirmed=True,
            rights_confirmed_at=utcnow(),
            clip_count=clip_count,
        )
        s.add(p)
        s.flush()
        pid = p.id
    if file and file.filename:
        stored = get_storage().save_stream(
            pid, "upload" + Path(file.filename).suffix.lower(), file.file
        )
        with session_scope() as s:
            s.get(Project, pid).source = str(stored)
    queue.enqueue("process_project", {}, project_id=pid)
    return get_project(pid)


@router.get("")
def list_projects() -> list[dict[str, Any]]:
    with session_scope() as s:
        return [project_dict(p) for p in s.scalars(select(Project).order_by(Project.id.desc()))]


@router.get("/{project_id}")
def get_project(project_id: int) -> dict[str, Any]:
    with session_scope() as s:
        p = s.get(Project, project_id)
        if not p:
            raise HTTPException(404, "Project not found")
        jobs = s.scalars(
            select(Job).where(Job.project_id == project_id).order_by(Job.id.desc())
        ).all()
        return {**project_dict(p), "jobs": [job_dict(j) for j in jobs]}


@router.get("/{project_id}/transcript")
def get_transcript(project_id: int) -> dict[str, Any]:
    with session_scope() as s:
        p = s.get(Project, project_id)
        if not p or not p.transcript_path or not Path(p.transcript_path).exists():
            raise HTTPException(404, "No transcript yet")
        import json

        return json.loads(Path(p.transcript_path).read_text(encoding="utf-8"))


class HostIn(BaseModel):
    speaker: str


@router.post("/{project_id}/host")
def confirm_host(project_id: int, body: HostIn) -> dict[str, Any]:
    """One click: set (or change) the host, then (re)pick clips with that host."""
    with session_scope() as s:
        p = s.get(Project, project_id)
        if not p:
            raise HTTPException(404, "Project not found")
        speakers = [st["speaker"] for st in (p.speakers or {}).get("stats", [])]
        if body.speaker not in speakers:
            raise HTTPException(422, f"Unknown speaker {body.speaker}; choose one of {speakers}")
        busy = s.scalars(
            select(Job).where(
                Job.project_id == project_id,
                Job.kind == "pick_clips",
                Job.status.in_(["queued", "running"]),
            )
        ).first()
        if busy:
            raise HTTPException(409, "Clips are being picked right now; try again when it ends")
        p.host_speaker = body.speaker
        p.host_confirmed = True
    queue.enqueue("pick_clips", {}, project_id=project_id)
    return get_project(project_id)


class BrollSourceIn(BaseModel):
    url: str
    rights_confirmed: bool
    license: str = ""


@router.put("/{project_id}/broll-sources")
def set_broll_sources(project_id: int, body: list[BrollSourceIn]) -> dict[str, Any]:
    """Permitted YouTube footage for this project's B-roll. Every entry needs rights."""
    entries = []
    for e in body:
        if not e.rights_confirmed:
            raise HTTPException(422, f"Confirm you have permission to use {e.url}")
        try:
            url = validate_url(e.url)
        except IngestError as err:
            raise HTTPException(422, str(err)) from err
        entries.append({"url": url, "rights_confirmed": True, "license": e.license[:200]})
    with session_scope() as s:
        p = s.get(Project, project_id)
        if not p:
            raise HTTPException(404, "Project not found")
        if p.source_type == "url" and any(
            x["url"].rstrip("/") == p.source.rstrip("/") for x in entries
        ):
            raise HTTPException(422, "The podcast itself can't be used as B-roll")
        p.broll_sources = entries
    return get_project(project_id)


@router.post("/{project_id}/export")
def export_ready_clips(project_id: int) -> dict[str, Any]:
    """Queue an export pack for every clip that passed the gate."""
    with session_scope() as s:
        if not s.get(Project, project_id):
            raise HTTPException(404, "Project not found")
        ready = [
            c.id
            for c in s.scalars(
                select(Clip).where(Clip.project_id == project_id).order_by(Clip.rank)
            )
            if c.status == "ready"
        ]
    jobs = [queue.enqueue("export_clip", {"clip_id": cid}, project_id=project_id) for cid in ready]
    return {"queued": len(jobs), "clip_ids": ready}


@router.get("/{project_id}/clips")
def get_clips(project_id: int) -> list[dict[str, Any]]:
    with session_scope() as s:
        clips = s.scalars(
            select(Clip).where(Clip.project_id == project_id).order_by(Clip.rank)
        ).all()
        return [clip_dict(c) for c in clips]
