"""Clip endpoints: details, (re)build, edits, video, waveform for the editor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from clipforge.api.serializers import clip_dict, job_dict
from clipforge.db import Clip, Job, session_scope, utcnow
from clipforge.jobs import queue
from clipforge.settings import get_settings
from clipforge.storage import get_storage

router = APIRouter(prefix="/api/clips", tags=["clips"])

ACTIVE = ("queued", "running", "cancel_requested")


def _clip_or_404(s, clip_id: int) -> Clip:
    c = s.get(Clip, clip_id)
    if not c:
        raise HTTPException(404, "Clip not found")
    return c


def _active_job(s, clip_id: int, kinds: tuple[str, ...]) -> Job | None:
    for j in s.scalars(select(Job).where(Job.kind.in_(kinds), Job.status.in_(ACTIVE))):
        if int((j.payload or {}).get("clip_id", -1)) == clip_id:
            return j
    return None


def _inside_project(path: str | None, project_id: int) -> Path:
    """Serve only files that live inside the project's own folder."""
    if not path:
        raise HTTPException(404, "Not rendered yet")
    p = Path(path).resolve()
    root = get_storage().project_dir(project_id).resolve()
    if root not in p.parents or not p.exists():
        raise HTTPException(404, "File not available")
    return p


@router.get("/{clip_id}")
def get_clip(clip_id: int) -> dict[str, Any]:
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        jobs = [
            j
            for j in s.scalars(
                select(Job).where(Job.project_id == c.project_id).order_by(Job.id.desc())
            )
            if int((j.payload or {}).get("clip_id", -1)) == clip_id
        ]
        return {**clip_dict(c, full=True), "jobs": [job_dict(j) for j in jobs[:10]]}


@router.post("/{clip_id}/build")
def build(clip_id: int) -> dict[str, Any]:
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        if _active_job(s, clip_id, ("build_clip",)):
            raise HTTPException(409, "This clip is already being built")
        project_id = c.project_id
    job_id = queue.enqueue("build_clip", {"clip_id": clip_id}, project_id=project_id)
    with session_scope() as s:
        return job_dict(s.get(Job, job_id))


class Trim(BaseModel):
    segment: int = Field(ge=0)
    start: float | None = None
    end: float | None = None


class CaptionFix(BaseModel):
    index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=60)


class BrollDecision(BaseModel):
    slot: int = Field(ge=0)
    action: str = Field(pattern="^(approve|clear|auto)$")
    candidate: int | None = Field(default=None, ge=0)


class EditsIn(BaseModel):
    trims: list[Trim] | None = None
    clear_trims: bool = False
    captions: list[CaptionFix] | None = None
    host_insert: bool | None = None
    broll: list[BrollDecision] | None = None


@router.patch("/{clip_id}/edits")
def update_edits(clip_id: int, body: EditsIn) -> dict[str, Any]:
    """Store owner edits. They take effect on the next build (POST /build)."""
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        edits = dict(c.edits or {})
        if body.clear_trims:
            edits.pop("trims", None)
        if body.trims:
            n_segments = len((c.plan or {}).get("plan", {}).get("segments", []))
            trims = dict(edits.get("trims") or {})
            for t in body.trims:
                if t.segment >= n_segments:
                    raise HTTPException(422, f"Clip has {n_segments} segment(s)")
                if t.start is not None and t.end is not None and t.end <= t.start:
                    raise HTTPException(422, "Trim end must be after its start")
                prev = trims.get(str(t.segment), [None, None])
                trims[str(t.segment)] = [
                    t.start if t.start is not None else prev[0],
                    t.end if t.end is not None else prev[1],
                ]
            edits["trims"] = trims
        if body.captions:
            fixes = dict(edits.get("captions") or {})
            for f in body.captions:
                fixes[str(f.index)] = f.text.strip()
            edits["captions"] = fixes
        if body.host_insert is not None:
            edits["host_insert"] = body.host_insert
        if body.broll:
            decisions = dict(edits.get("broll") or {})
            slots = {b["index"]: b for b in (c.spec or {}).get("broll", [])}
            for d in body.broll:
                if d.slot not in slots:
                    raise HTTPException(422, f"No B-roll slot {d.slot}")
                if d.action == "auto":
                    decisions.pop(str(d.slot), None)
                elif d.action == "clear":
                    decisions[str(d.slot)] = {"action": "clear"}
                else:
                    n = len(slots[d.slot].get("candidates", []))
                    if d.candidate is None or d.candidate >= n:
                        raise HTTPException(422, f"Slot {d.slot} has {n} candidate(s)")
                    decisions[str(d.slot)] = {"action": "approve", "candidate": d.candidate}
            edits["broll"] = decisions
        c.edits = edits
        c.updated_at = utcnow()
        return clip_dict(c, full=True)


def _enqueue_for_clip(clip_id: int, kind: str) -> dict[str, Any]:
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        if _active_job(s, clip_id, (kind,)):
            raise HTTPException(409, "Already queued for this clip")
        project_id = c.project_id
    job_id = queue.enqueue(kind, {"clip_id": clip_id}, project_id=project_id)
    with session_scope() as s:
        return job_dict(s.get(Job, job_id))


@router.post("/{clip_id}/copy")
def regenerate_copy(clip_id: int) -> dict[str, Any]:
    return _enqueue_for_clip(clip_id, "write_copy")


class CopyIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    youtube_description: str = Field(max_length=5000)
    instagram_caption: str = Field(max_length=2200)
    instagram_tags: list[str] = Field(max_length=30)
    x_post: str = Field(max_length=500)


@router.put("/{clip_id}/copy")
def edit_copy(clip_id: int, body: CopyIn) -> dict[str, Any]:
    """Owner edits to the copy pack; the same rules apply (one emoji in the title, max 5
    tags with the brand tag first, no emojis on X, ...)."""
    from clipforge.app_settings import load_config
    from clipforge.core.copywriter import CopyInvalid, normalize

    cfg = load_config()
    try:
        pack = normalize(body.model_dump(), cfg.brand_tag, cfg.credit_line)
    except CopyInvalid as e:
        raise HTTPException(422, str(e)) from e
    pack.edited = True
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        pack.generated_by = (c.copy_pack or {}).get("generated_by", "owner")
        c.copy_pack = pack.model_dump()
        c.updated_at = utcnow()
        return clip_dict(c, full=True)


@router.post("/{clip_id}/export")
def export(clip_id: int) -> dict[str, Any]:
    return _enqueue_for_clip(clip_id, "export_clip")


@router.get("/{clip_id}/export")
def download_export(clip_id: int) -> FileResponse:
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        path = c.export_path
    if not path:
        raise HTTPException(404, "Not exported yet")
    p = Path(path).resolve()
    root = (get_settings().data_dir / "exports").resolve()
    if root not in p.parents or not p.exists():
        raise HTTPException(404, "Export file not available")
    return FileResponse(p, media_type="application/zip", filename=p.name)


@router.get("/{clip_id}/video")
def video(clip_id: int) -> FileResponse:
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        path = _inside_project(c.render_path, c.project_id)
    return FileResponse(path, media_type="video/mp4", filename=f"clip-{clip_id}.mp4")


@router.get("/{clip_id}/broll/{slot}/sheet")
def contact_sheet(clip_id: int, slot: int, candidate: int | None = None) -> FileResponse:
    """The contact sheet of the chosen shot (or of candidate k) for the review screen."""
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        slots = {b["index"]: b for b in (c.spec or {}).get("broll", [])}
        project_id = c.project_id
    if slot not in slots:
        raise HTTPException(404, "No such slot")
    b = slots[slot]
    if candidate is None:
        path = (b.get("choice") or {}).get("contact_sheet")
    else:
        cands = b.get("candidates", [])
        path = cands[candidate]["contact_sheet"] if candidate < len(cands) else None
    return FileResponse(_inside_project(path, project_id), media_type="image/jpeg")


@router.post("/{clip_id}/broll/{slot}/upload")
def upload_cover(clip_id: int, slot: int, file: UploadFile = File(...)) -> dict[str, Any]:
    """The owner's own footage for a slot (manual cover). Checked on the next build."""
    if Path(file.filename or "").suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm"):
        raise HTTPException(422, "Upload a video file (.mp4, .mov, .mkv, .webm)")
    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        if slot not in {b["index"] for b in (c.spec or {}).get("broll", [])}:
            raise HTTPException(404, "No such slot")
        project_id = c.project_id
    folder = get_storage().project_dir(project_id) / "manual_broll"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"clip{clip_id}_slot{slot}{Path(file.filename).suffix.lower()}"
    with dest.open("wb") as out:
        while chunk := file.file.read(1 << 20):
            out.write(chunk)
    with session_scope() as s:
        c = s.get(Clip, clip_id)
        edits = dict(c.edits or {})
        decisions = dict(edits.get("broll") or {})
        decisions[str(slot)] = {"action": "manual", "path": str(dest)}
        edits["broll"] = decisions
        c.edits = edits
        c.updated_at = utcnow()
        return clip_dict(c, full=True)


@router.get("/{clip_id}/waveform")
def waveform(clip_id: int, pad: float = 3.0, points_per_second: int = 50) -> dict[str, Any]:
    """RMS envelope of the episode around the clip (with `pad` s either side), for the trim
    handles. Values are dBFS per bucket; times are episode seconds."""
    import numpy as np

    from clipforge.core.audio import rms_trace
    from clipforge.pipeline.clip_tasks import _loaded_audio

    with session_scope() as s:
        c = _clip_or_404(s, clip_id)
        project_id = c.project_id
        segs = (c.plan or {}).get("plan", {}).get("segments", [])
        spec = c.spec
    wav = get_storage().project_dir(project_id) / "audio48k.wav"
    if not wav.exists():
        raise HTTPException(404, "Audio not prepared yet; build the clip first")
    audio = _loaded_audio(str(wav), wav.stat().st_mtime)
    start = min(s["start"] for s in segs) - pad
    end = max(s["end"] for s in segs) + pad
    if spec:
        start = min(start, spec["pieces"][0]["src_start"] - pad)
        end = max(end, spec["pieces"][-1]["src_end"] + pad)
    start, end = max(0.0, start), min(len(audio) / 48000, end)
    hop = 1.0 / max(10, min(points_per_second, 200))
    seg = audio[int(start * 48000) : int(end * 48000)]
    trace = rms_trace(seg, 48000, hop=hop)
    return {
        "start": start,
        "hop": hop,
        "db": np.round(trace, 1).tolist(),
        "pieces": spec["pieces"] if spec else [],
    }
