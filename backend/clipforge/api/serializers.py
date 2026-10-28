from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any

from clipforge.db import Clip, Job, JobEvent, Project


def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "project_id": job.project_id,
        "payload": job.payload,
        "result": job.result,
        "qa": job.qa or [],
        "error": job.error,
        "attempts": job.attempts,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def event_dict(ev: JobEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "job_id": ev.job_id,
        "at": _iso(ev.at),
        "level": ev.level,
        "message": ev.message,
        "progress": ev.progress,
    }


def project_dict(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "source_type": p.source_type,
        "source": p.source if p.source_type == "url" else PureWindowsPath(p.source).name,
        "rights_confirmed": p.rights_confirmed,
        "status": p.status,
        "clip_count": p.clip_count,
        "media_info": p.media_info,
        "speakers": p.speakers,
        "host_speaker": p.host_speaker,
        "host_suggestion": p.host_suggestion,
        "host_confirmed": p.host_confirmed,
        "broll_sources": p.broll_sources or [],
        "created_at": _iso(p.created_at),
    }


def clip_dict(c: Clip, full: bool = False) -> dict[str, Any]:
    d = {
        "id": c.id,
        "project_id": c.project_id,
        "rank": c.rank,
        "plan": c.plan,
        "qa": c.qa or [],
        "status": c.status,
        "error": c.error,
        "verified": c.verified,
        "has_render": bool(c.render_path),
        "has_export": bool(c.export_path),
        "copy_pack": c.copy_pack,
        "edits": c.edits or {},
        "updated_at": _iso(c.updated_at),
    }
    if full:
        d["spec"] = c.spec
    return d
