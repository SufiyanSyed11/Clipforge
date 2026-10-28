"""Per-clip pipeline: plan -> cut in silence -> voice -> render -> verification gate.

`build_clip` is re-run after every owner edit (trim, caption fix, B-roll swap, host-insert
toggle). Intermediates live in the clip's folder; a cancelled or crashed build is simply run
again.
"""

from __future__ import annotations

import contextvars
import functools
import json
from pathlib import Path

import numpy as np
from sqlalchemy import select

from clipforge.app_settings import AppConfig, load_config
from clipforge.broll.engine import BrollContext
from clipforge.core import captions, faces, framing, ingest, verify
from clipforge.core.audio import load_wav48, write_wav
from clipforge.core.cut import CutConfig, CutEngine, CutError
from clipforge.core.mix import MixError, mix
from clipforge.core.render import RenderCancelled, RenderError, render, video_size
from clipforge.core.voice import assemble_voice
from clipforge.db import Clip, Project, session_scope, utcnow
from clipforge.editspec import EditSpec
from clipforge.jobs.registry import JobContext, TaskCancelled, TaskError, TaskResult, task
from clipforge.models import PickedClip, Transcript
from clipforge.qa import QAReport
from clipforge.settings import get_settings
from clipforge.storage import get_storage

# The running build's B-roll context (with its job logger and cancel check).
_BROLL_CTX: contextvars.ContextVar[BrollContext | None] = contextvars.ContextVar(
    "broll_ctx", default=None
)


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


def analysis_path(project_id: int) -> Path:
    return get_storage().project_dir(project_id) / "faces.json"


@functools.lru_cache(maxsize=2)
def _loaded_analysis(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_analysis(project_id: int) -> dict | None:
    p = analysis_path(project_id)
    return _loaded_analysis(str(p), p.stat().st_mtime) if p.exists() else None


@task("analyze_video")
def analyze_video(ctx: JobContext) -> TaskResult:
    """Faces, shots and lip movement over the whole episode (once per project)."""
    with session_scope() as s:
        project = s.get(Project, ctx.project_id)
    if not project or not project.media_path:
        raise TaskError("No media to analyse")
    if not (project.media_info or {}).get("has_video"):
        return TaskResult(result={"skipped": "audio only"})
    out = analysis_path(project.id)
    if out.exists():
        ctx.log("Face analysis already done")
        return TaskResult(result={"cached": True})
    cfg = load_config()
    sface = None
    if cfg.face_recognition and not get_settings().fake_models:
        sface = faces.ensure_sface(get_settings().data_dir / "models")
        ctx.log(
            "Face recognition model ready"
            if sface
            else "Face recognition model unavailable: identities will follow the speakers"
        )
    detector = faces.YuNetDetector(sface)
    sw, _ = video_size(project.media_path)
    ctx.log("Analysing faces and shots (5 fps)")
    try:
        result = faces.analyze_samples(
            faces.decode_samples(project.media_path),
            detector,
            scale_to_source=sw / faces.ANALYSIS_WIDTH,
            log=lambda m: ctx.log(m),
            cancelled=ctx.cancelled,
        )
    except InterruptedError:
        raise TaskCancelled() from None
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result), encoding="utf-8")
    tmp.replace(out)
    n_faces = sum(len(x["faces"]) for x in result["samples"])
    qa = QAReport(step="face-analysis")
    qa.check(
        "faces found",
        n_faces > 0,
        f"{n_faces} detections in {len(result['samples'])} samples, {result['shots']} shot(s)",
        severity="warn",
    )
    qa.check(
        "face recognition across shots",
        sface is not None,
        "SFace" if sface else "not available: host found by lip movement only",
        severity="warn",
    )
    return TaskResult(result={"samples": len(result["samples"])}, qa=[qa])


def used_host_shots(project_id: int, clip_id: int) -> set[str]:
    with session_scope() as s:
        rows = s.scalars(select(Clip).where(Clip.project_id == project_id, Clip.id != clip_id))
        return {
            (c.spec or {}).get("host_insert", {}).get("shot_id")
            for c in rows
            if (c.spec or {}).get("host_insert")
        } - {None}


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
    spec.audio_only = not (project.media_info or {}).get("has_video", True)
    qa = [res.qa]

    # Captions: corrected words, emphasis, censoring.
    spec.captions, cap_qa = captions.prepare_captions(
        spec, edits.get("captions") or {}, picked.plan.hook, picked.plan.payoff
    )
    qa.append(cap_qa)

    # Framing and the host-reaction insert.
    if not spec.audio_only:
        analysis = load_analysis(project.id)
        ident = faces.identify(analysis, transcript, project.host_speaker) if analysis else None
        sw, sh = video_size(project.media_path)
        spec.camera, cam_qa = framing.plan_camera(spec, analysis, ident, transcript, sw, sh)
        qa.append(cam_qa)
        avoid = [(b.out_start, b.out_end) for b in spec.broll if b.status == "filled"]
        insert, ins_qa = framing.find_host_insert(
            spec,
            analysis,
            ident,
            transcript,
            project.host_speaker,
            used_host_shots(project.id, clip.id),
            avoid,
            sw,
            sh,
        )
        spec.host_insert = insert
        qa.append(ins_qa)

    # B-roll: beats after the hook, clear of the host insert, filled only by checked footage.
    spec.broll, broll_qa = plan_broll(spec, clip, project, picked, cfg)
    qa.append(broll_qa)
    return spec, qa


def broll_context(project: Project, cfg: AppConfig, log, cancelled) -> BrollContext:
    from clipforge.broll import media
    from clipforge.broll.sources import (
        LibrarySource,
        PermittedYouTubeSource,
        PexelsSource,
        PixabaySource,
    )
    from clipforge.llm import get_vision_provider

    settings = get_settings()
    data = settings.data_dir
    sources = [
        s
        for s in (
            PexelsSource(settings.pexels_api_key),
            PixabaySource(settings.pixabay_api_key),
            PermittedYouTubeSource(project.broll_sources or [], data / "broll_cache"),
            LibrarySource(data / "broll_library"),
        )
        if s.available()
    ]
    vprov = get_vision_provider()
    vstatus = vprov.check()
    pdir = get_storage().project_dir(project.id)
    hashes_file = pdir / "podcast_hashes.json"
    if hashes_file.exists():
        hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
    elif (project.media_info or {}).get("has_video"):
        hashes = media.podcast_hashes(Path(project.media_path), project.media_info["duration"])
        hashes_file.write_text(json.dumps(hashes), encoding="utf-8")
    else:
        hashes = []
    return BrollContext(
        sources=sources,
        vision=vprov if vstatus.ok else None,
        reject_rules=cfg.broll_reject_rules,
        banned=cfg.broll_banned_content,
        cache=data / "broll_cache",
        sheets=pdir / "contact_sheets",
        podcast_hashes=hashes,
        podcast_url=project.source if project.source_type == "url" else None,
        podcast_sha1=None,
        log=log,
        cancelled=cancelled,
    )


def plan_broll(spec: EditSpec, clip: Clip, project: Project, picked: PickedClip, cfg: AppConfig):
    from clipforge.broll import beats, engine, queries
    from clipforge.editspec import BrollSlot
    from clipforge.llm import get_text_provider

    slots = beats.plan_slots(spec)
    ctx = _BROLL_CTX.get()
    if ctx is None:
        ctx = broll_context(project, cfg, lambda m: None, lambda: False)
    prev = {s["beat_text"]: BrollSlot.model_validate(s) for s in (clip.spec or {}).get("broll", [])}
    ctx.previous = prev
    ctx.decisions = (clip.edits or {}).get("broll") or {}
    need_queries = [s for s in slots if s.beat_text not in prev]
    if need_queries:
        provider = get_text_provider()
        st = provider.check()
        filled, method = queries.generate(
            need_queries,
            picked.plan.hook + " " + picked.plan.payoff,
            picked.plan.broll_beats,
            provider if st.ok else None,
            ctx.log,
        )
        by_i = {s.index: s for s in filled}
        slots = [by_i.get(s.index, s) for s in slots]
        ctx.log(f"B-roll search terms: {method}")
    return engine.fill_slots(slots, ctx)


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
        token = _BROLL_CTX.set(broll_context(project, cfg, lambda m: ctx.log(m), ctx.cancelled))
        try:
            spec, plan_qa = plan_spec(clip, project, transcript, cfg, audio48)
        except InterruptedError:
            raise TaskCancelled() from None
        finally:
            _BROLL_CTX.reset(token)
        qa += plan_qa
        ctx.check_cancelled()

        voice = assemble_voice(audio48, spec)
        voice_path = write_wav(d / "voice.wav", voice)
        transcriber, real = make_transcriber()
        ctx.log("Verification: re-transcribing the cut audio")
        qa.append(verify.verify_voice(voice, spec, transcriber, transcriber_is_real=real))
        ctx.check_cancelled()

        ctx.log("Mixing: voice -14 LUFS, music 19 dB under (ducked), limiter -1 dB")
        mixed = d / "mix.wav"
        sfx_times = (
            [b.out_start for b in spec.broll if b.status == "filled"]
            if cfg.sfx_on_transitions
            else None
        )
        qa.append(
            mix(
                voice_path,
                mixed,
                bgm=Path(cfg.bgm_path) if cfg.bgm_path else None,
                sfx=Path(cfg.sfx_path) if cfg.sfx_path else None,
                sfx_times=sfx_times,
                duration=spec.duration,
                workdir=d / "mix",
            )
        )
        accent = cfg.caption_accent.lstrip("#")
        ass = captions.write_ass(
            spec,
            d / "captions.ass",
            font=cfg.caption_font,
            uppercase=cfg.caption_uppercase,
            accent_bgr=accent[4:6] + accent[2:4] + accent[0:2],
        )
        (d / "captions.srt").write_text(captions.srt_document(spec), encoding="utf-8")
        ctx.log(f"Rendering {spec.duration:.1f} s at {spec.width}x{spec.height} {spec.fps} fps")
        out = render(
            spec,
            mixed,
            d / "render.mp4",
            ass_path=ass,
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
    except (RenderError, MixError) as e:
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
    with session_scope() as s:
        needs_copy = s.get(Clip, clip_id).copy_pack is None
    if needs_copy:
        from clipforge.jobs import queue

        queue.enqueue("write_copy", {"clip_id": clip_id}, project_id=project.id)
    return TaskResult(result={"clip_id": clip_id, "verified": passed}, qa=qa)


def _clip_and_project(clip_id: int) -> tuple[Clip, Project]:
    with session_scope() as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            raise TaskError(f"Clip {clip_id} not found")
        return clip, s.get(Project, clip.project_id)


@task("write_copy")
def write_copy_task(ctx: JobContext) -> TaskResult:
    from clipforge.core import copywriter
    from clipforge.core.learn import learning_context
    from clipforge.llm import get_text_provider

    clip_id = int(ctx.payload["clip_id"])
    clip, _project = _clip_and_project(clip_id)
    if not clip.spec:
        raise TaskError("Build the clip before writing its copy")
    spec = EditSpec.model_validate(clip.spec)
    picked = PickedClip.model_validate(clip.plan)
    cfg = load_config()
    provider = get_text_provider()
    status = provider.check()
    if not status.ok:
        raise TaskError(f"Copy writer unavailable: {status.message()}")
    ctx.log(f"Writing the copy pack with {provider.name}:{provider.model}")
    try:
        pack, qa = copywriter.write_copy(
            provider,
            " ".join(c.spoken for c in spec.captions),
            picked.plan.hook,
            picked.plan.payoff,
            picked.plan.title_options,
            cfg.brand_tag,
            cfg.credit_line,
            lambda m: ctx.log(m),
            learning=learning_context(),
        )
    except copywriter.CopyInvalid as e:
        raise TaskError(str(e)) from e
    _set(clip_id, copy_pack=pack.model_dump())
    return TaskResult(result={"title": pack.title}, qa=[qa])


@task("export_clip")
def export_clip(ctx: JobContext) -> TaskResult:
    from clipforge.broll import engine
    from clipforge.core import copywriter, export

    clip_id = int(ctx.payload["clip_id"])
    clip, project = _clip_and_project(clip_id)
    if not clip.spec or not clip.render_path:
        raise TaskError("Build the clip before exporting it")
    spec = EditSpec.model_validate(clip.spec)
    cfg = load_config()
    bctx = broll_context(project, cfg, lambda m: ctx.log(m), ctx.cancelled)
    reverify = engine.reverify(spec.broll, bctx)
    pack = copywriter.CopyPack.model_validate(clip.copy_pack) if clip.copy_pack else None
    reasons = export.export_gate(
        clip.verified, clip.qa or [], reverify, pack, bool(clip.render_path)
    )
    unverified_test = False
    if reasons:
        only_gate = all(r.startswith("verification gate failed") for r in reasons)
        if get_settings().fake_models and only_gate and pack is not None:
            # Plumbing tests can't pass the gate (no real models); never in real runs.
            unverified_test = True
            ctx.log("FAKE_MODELS: exporting an UNVERIFIED TEST pack", level="warn")
        else:
            raise TaskError("Export blocked: " + "; ".join(reasons), [reverify])
    d = clip_dir(project.id, clip_id)
    folder, zpath = export.write_pack(
        get_settings().data_dir / "exports",
        project.name,
        clip.rank,
        spec,
        Path(clip.render_path),
        pack,
        (clip.qa or []) + [reverify.to_dict()],
        d,
        unverified_test=unverified_test,
    )
    _set(clip_id, export_path=str(zpath))
    ctx.log(f"Export pack: {folder}")
    return TaskResult(
        result={"folder": str(folder), "zip": str(zpath), "unverified_test": unverified_test},
        qa=[reverify],
    )
