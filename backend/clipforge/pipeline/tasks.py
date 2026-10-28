"""Project pipeline tasks.

`process_project`: ingest -> probe -> audio -> transcribe -> diarize -> host suggestion, then
waits for the owner to confirm the host. `pick_clips` runs after that confirmation.
Intermediates are cached in the project folder, so a re-run skips finished steps.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import delete

from clipforge.app_settings import load_config
from clipforge.core import diarize, ingest, pick, transcribe
from clipforge.db import Clip, Project, session_scope
from clipforge.jobs import queue
from clipforge.jobs.registry import JobContext, TaskError, TaskResult, task
from clipforge.llm import get_text_provider
from clipforge.models import Transcript
from clipforge.qa import QAReport
from clipforge.settings import get_settings
from clipforge.storage import get_storage
from clipforge.tools.check_env import load_report


def _project(project_id: int) -> Project:
    with session_scope() as s:
        p = s.get(Project, project_id)
        if not p:
            raise TaskError(f"Project {project_id} not found")
        return p


def _update(project_id: int, **fields) -> None:
    with session_scope() as s:
        p = s.get(Project, project_id)
        for k, v in fields.items():
            setattr(p, k, v)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def free_llm_vram(ctx: JobContext) -> None:
    """Whisper and a local LLM don't both fit in 6 GB of VRAM: ask Ollama to unload first."""
    provider = get_text_provider()
    unload = getattr(provider, "unload", None)
    if unload:
        unload()
        ctx.log("Asked Ollama to free GPU memory before transcription")


def diarization_backend(settings, env_report: dict | None) -> str:
    if settings.diarization_backend != "auto":
        return settings.diarization_backend
    return (env_report or {}).get("defaults", {}).get("diarization_backend", "basic")


def run_ingest_and_transcribe(
    project_id: int, ctx: JobContext
) -> tuple[Transcript, list[QAReport]]:
    settings = get_settings()
    p = _project(project_id)
    ingest.require_rights(p.rights_confirmed)
    pdir = get_storage().project_dir(project_id)
    qa: list[QAReport] = []

    # 1. media
    media = Path(p.media_path) if p.media_path else None
    if media is None or not media.exists():
        if p.source_type == "url":
            ctx.log(f"Ingest: {p.source}")
            media = ingest.download(p.source, pdir / "source", lambda m: ctx.log(m))
        else:
            media = Path(p.source)
            if not media.exists():
                raise TaskError(f"Uploaded file is missing: {media.name}")
        _update(project_id, media_path=str(media))
    info = ingest.probe(media)
    ingest_report = ingest.ingest_qa(info)
    qa.append(ingest_report)
    _update(project_id, media_info=info.model_dump())
    ctx.log(
        f"Media: {info.duration / 60:.1f} min, "
        + (f"{info.width}x{info.height} @ {info.fps} fps" if info.has_video else "audio only"),
        progress=0.1,
    )
    if not ingest_report.passed:
        raise TaskError(
            "Ingest QA failed: "
            + "; ".join(f"{c.name} ({c.detail})" for c in ingest_report.failures),
            qa,
        )

    # 2. audio for whisper + diarization
    wav = pdir / "audio16k.wav"
    if not wav.exists():
        ctx.log("Extracting 16 kHz mono audio")
        ingest.extract_audio(media, wav)

    # 3. transcript (cached per model)
    env = load_report()
    if settings.fake_models:
        cfg_name = "fake"
    else:
        cfg = transcribe.resolve_config(settings, env)
        cfg_name = f"{cfg.model}-{cfg.compute_type}"
        ctx.log(f"Whisper config: {cfg.model} on {cfg.device} ({cfg.compute_type}). {cfg.reason}")
    raw_path = pdir / f"transcript.raw.{cfg_name}.json"
    if raw_path.exists():
        ctx.log("Using cached transcript")
        t = Transcript.model_validate_json(raw_path.read_text(encoding="utf-8"))
    elif settings.fake_models:
        from clipforge.testing import fake_asr

        ctx.log("FAKE_MODELS: test-double transcription (plumbing test, not a real result)")
        audio16, _ = ingest.load_wav(wav)
        t = fake_asr.transcribe(audio16, 16000, info.duration)
    else:
        if cfg.device == "cuda":
            free_llm_vram(ctx)
        t = transcribe.transcribe_file(wav, cfg, lambda m, prog: ctx.log(m, progress=prog))
    _atomic_write(raw_path, t.model_dump_json())
    qa.append(transcribe.transcript_qa(t))

    # 4. speakers
    backend = "fake" if settings.fake_models else diarization_backend(settings, env)
    ctx.log(f"Diarization backend: {backend}")
    if backend == "fake":
        dia = diarize.fake_turns(t)
    elif backend == "pyannote":
        if not settings.hf_token:
            raise TaskError("DIARIZATION_BACKEND=pyannote needs HF_TOKEN in .env")
        dia = diarize.diarize_pyannote(str(wav), settings.hf_token)
    else:
        audio, _ = ingest.load_wav(wav)
        dia = diarize.diarize_basic(audio, t)
    t = diarize.assign_speakers(t, dia.turns)
    stats = diarize.speaker_stats(t)
    suggestion = diarize.suggest_host(t, stats)
    qa.append(diarize.diarize_qa(t, stats, backend, dia.separation))
    final = pdir / "transcript.json"
    _atomic_write(final, t.model_dump_json())
    (pdir / "transcript.txt").write_text(pick.format_transcript(t, None), encoding="utf-8")
    _update(
        project_id,
        transcript_path=str(final),
        speakers={
            "backend": backend,
            "separation": dia.separation,
            "stats": [s.model_dump() for s in stats],
        },
        host_suggestion=suggestion.model_dump() if suggestion else None,
    )
    if suggestion:
        ctx.log(
            f"Suggested host: {suggestion.speaker} ({suggestion.confidence} confidence: "
            f"{', '.join(suggestion.reasons)})"
        )
    return t, qa


@task("process_project")
def process_project(ctx: JobContext) -> TaskResult:
    pid = ctx.project_id
    _update(pid, status="processing")
    try:
        require_text_provider(ctx)
        t, qa = run_ingest_and_transcribe(pid, ctx)
    except ingest.RightsNotConfirmed as e:
        _update(pid, status="failed")
        raise TaskError(str(e)) from e
    except (ingest.IngestError, TaskError) as e:
        _update(pid, status="failed")
        raise TaskError(str(e), getattr(e, "qa", None)) from e
    except Exception:
        _update(pid, status="failed")
        raise
    _update(pid, status="awaiting_host")
    ctx.log("Waiting for the host to be confirmed in the UI", progress=1.0)
    return TaskResult(
        result={"words": len(t.words), "segments": len(t.segments), "duration": t.duration},
        qa=qa,
    )


def require_text_provider(ctx: JobContext):
    """Fail fast (before any long step) if the clip-picking model can't be used."""
    provider = get_text_provider()
    status = provider.check()
    if not status.ok:
        raise TaskError(f"Clip picker unavailable: {status.message()}")
    ctx.log(("" if status.real else "TEST DOUBLE: ") + f"Clip picker: {status.detail}")
    return provider


@task("pick_clips")
def pick_clips_task(ctx: JobContext) -> TaskResult:
    pid = ctx.project_id
    p = _project(pid)
    if not p.transcript_path or not Path(p.transcript_path).exists():
        raise TaskError("No transcript yet; run processing first")
    if not p.host_confirmed:
        raise TaskError("Confirm the host before picking clips")
    _update(pid, status="picking")
    t = Transcript.model_validate_json(Path(p.transcript_path).read_text(encoding="utf-8"))
    cfg = load_config()
    settings = get_settings()
    try:
        provider = require_text_provider(ctx)
    except TaskError:
        _update(pid, status="failed")
        raise
    try:
        result = pick.pick_clips(
            t,
            p.host_speaker,
            provider,
            lambda m: ctx.log(m),
            n=p.clip_count,
            style=cfg.style.model_dump(),
            prompt_version=settings.picker_prompt_version,
            max_retries=settings.picker_max_retries,
        )
    except pick.PickerError as e:
        _update(pid, status="failed")
        raise TaskError(str(e)) from e
    qa = pick.pick_qa(result, p.clip_count, t)
    pdir = get_storage().project_dir(pid)
    _atomic_write(
        pdir / "clips.json",
        json.dumps(
            {
                "host_speaker": p.host_speaker,
                "prompt_version": settings.picker_prompt_version,
                "provider": result.provider,
                "model": result.model,
                "real": result.real,
                "clips": [c.model_dump() for c in result.clips],
                "rejected": result.rejected,
            },
            indent=2,
        ),
    )
    with session_scope() as s:
        s.execute(delete(Clip).where(Clip.project_id == pid))
        new = [Clip(project_id=pid, rank=c.rank, plan=c.model_dump()) for c in result.clips]
        s.add_all(new)
        s.flush()
        clip_ids = [c.id for c in new]
    _update(pid, status="clips_ready")
    for cid in clip_ids:
        queue.enqueue("build_clip", {"clip_id": cid}, project_id=pid)
    ctx.log(f"{len(result.clips)} clips picked; building each one", progress=1.0)
    return TaskResult(
        result={"clips": len(result.clips), "rejected": len(result.rejected)}, qa=[qa]
    )
