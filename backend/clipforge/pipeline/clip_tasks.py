"""Per-clip pipeline: plan -> cut in silence -> voice -> render -> verification gate.

`build_clip` is re-run after every owner edit (trim, caption fix, B-roll swap, host-insert
toggle). Intermediates live in the clip's folder; a cancelled or crashed build is simply run
again.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import numpy as np

from clipforge.app_settings import AppConfig, load_config
from clipforge.core import ingest, verify
from clipforge.core.audio import load_wav48, write_wav
from clipforge.core.cut import CutConfig, CutEngine, CutError
from clipforge.core.render import RenderCancelled, RenderError, render
from clipforge.core.voice import assemble_voice
from clipforge.db import Clip, Project, session_scope, utcnow
from clipforge.editspec import EditSpec
from clipforge.jobs.registry import JobContext, TaskCancelled, TaskError, TaskResult, task
from clipforge.models import PickedClip, Transcript
from clipforge.qa import QAReport
from clipforge.settings import get_settings
from clipforge.storage import get_storage


def clip_dir(project_id: int, clip_id: int) -> Path:
    d = get_storage().project_dir(project_id) / "clips" / f"{clip_id:04d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio48_path(project_id: int, media: Path) -> Path:
    wav = get_storage().project_dir(project_id) / "audio48k.wav"
    if not wav.exists():
        ingest.extract_audio(media, wav, sample_rate=48000)
    return wav


@functools.lru_cache(maxsize=2)
def _loaded_audio(path: str, mtime: float) -> np.ndarray:
    return load_wav48(Path(path))


def cut_config(cfg: AppConfig) -> CutConfig:
    return CutConfig(
        fps=cfg.style.fps,
        min_clip=cfg.style.min_clip_seconds,
        max_clip=min(cfg.style.max_clip_seconds, 30.0),
        profanity_mode=cfg.profanity_mode,
    )


def make_transcriber():
    """(transcriber, is_real) for the verification gate's re-transcription."""
    settings = get_settings()
    if settings.fake_models:
        from clipforge.testing import fake_asr

        return fake_asr.heard_words, False
    from clipforge.core.transcribe import resolve_config
    from clipforge.tools.check_env import load_report

    cfg = resolve_config(settings, load_report())
    return verify.whisper_transcriber(cfg), True


def _set(clip_id: int, **fields) -> None:
    with session_scope() as s:
        c = s.get(Clip, clip_id)
        for k, v in fields.items():
            setattr(c, k, v)
        c.updated_at = utcnow()


def plan_spec(
    clip: Clip, project: Project, transcript: Transcript, cfg: AppConfig, audio48: np.ndarray
) -> tuple[EditSpec, list[QAReport]]:
    """Everything up to (not including) rendering. Later phases add framing, captions,
    B-roll and the mix to the spec here."""
    picked = PickedClip.model_validate(clip.plan)
    edits = clip.edits or {}
    trims = {
        int(k): (v[0], v[1]) for k, v in (edits.get("trims") or {}).items() if isinstance(v, list)
    }
    engine = CutEngine(audio48, transcript, cut_config(cfg))
    res = engine.build(
        [(s.start, s.end) for s in picked.plan.segments],
        source_path=project.media_path,
        source_fps=(project.media_info or {}).get("fps"),
        trims=trims,
    )
    spec = res.spec
    spec.clip_id = clip.id
    spec.width, spec.height, spec.fps = cfg.style.width, cfg.style.height, cfg.style.fps
    spec.frame_rate_mode = cfg.frame_rate_mode
    spec.grade = cfg.grade
    spec.logo_path = cfg.logo_path
    spec.host_insert_enabled = bool(edits.get("host_insert", True))
    return spec, [res.qa]


@task("build_clip")
def build_clip(ctx: JobContext) -> TaskResult:
    clip_id = int(ctx.payload["clip_id"])
    with session_scope() as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            raise TaskError(f"Clip {clip_id} not found")
        project = s.get(Project, clip.project_id)
    if not project.media_path or not project.transcript_path:
        raise TaskError("The project has no media/transcript yet")
    _set(clip_id, status="building", error=None)
    cfg = load_config()
    d = clip_dir(project.id, clip_id)
    qa: list[QAReport] = []
    try:
        transcript = Transcript.model_validate_json(
            Path(project.transcript_path).read_text(encoding="utf-8")
        )
        wav = audio48_path(project.id, Path(project.media_path))
        audio48 = _loaded_audio(str(wav), wav.stat().st_mtime)
        ctx.log(f"Clip #{clip.rank}: cutting in measured silence")
        spec, plan_qa = plan_spec(clip, project, transcript, cfg, audio48)
        qa += plan_qa
        ctx.check_cancelled()

        voice = assemble_voice(audio48, spec)
        voice_path = write_wav(d / "voice.wav", voice)
        transcriber, real = make_transcriber()
        ctx.log("Verification: re-transcribing the cut audio")
        qa.append(verify.verify_voice(voice, spec, transcriber, transcriber_is_real=real))
        ctx.check_cancelled()

        audio_for_render = voice_path
        ctx.log(f"Rendering {spec.duration:.1f} s at {spec.width}x{spec.height} {spec.fps} fps")
        out = render(
            spec,
            audio_for_render,
            d / "render.mp4",
            encoder=get_settings().render_encoder,
            log=lambda m, p: ctx.log(m, progress=p),
            cancelled=ctx.cancelled,
        )
        qa.append(verify.verify_render(out, spec))
    except CutError as e:
        at = f" (at {e.at_seconds:.2f} s in the episode)" if e.at_seconds is not None else ""
        _set(clip_id, status="failed", error=f"{e}{at}", qa=[r.to_dict() for r in qa])
        raise TaskError(f"{e}{at}", qa) from e
    except (RenderCancelled, TaskCancelled):
        _set(clip_id, status="picked", error="Build cancelled")
        raise TaskCancelled() from None
    except RenderError as e:
        _set(clip_id, status="failed", error=str(e), qa=[r.to_dict() for r in qa])
        raise TaskError(str(e), qa) from e

    passed = all(r.passed for r in qa)
    (d / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    (d / "qa.json").write_text(json.dumps([r.to_dict() for r in qa], indent=2), encoding="utf-8")
    _set(
        clip_id,
        status="ready" if passed else "needs_review",
        spec=spec.model_dump(),
        render_path=str(out),
        verified=passed,
        qa=[r.to_dict() for r in qa],
        error=None
        if passed
        else "; ".join(
            f"{c.name}" + (f" @ {c.at_seconds:.2f} s" if c.at_seconds is not None else "")
            for r in qa
            for c in r.failures
        )[:2000],
    )
    ctx.log("Gate passed" if passed else "Gate FAILED: see the QA checklist", progress=1.0)
    return TaskResult(result={"clip_id": clip_id, "verified": passed}, qa=qa)
