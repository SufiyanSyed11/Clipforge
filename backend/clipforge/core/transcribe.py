"""Transcription with faster-whisper: word timestamps, VAD, CPU chunking.

Whisper word times can be 0.1-0.4 s off in loud or noisy stretches. They are used to find
*where* words are; Phase 2 decides every actual cut from the audio's RMS, never from these.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from clipforge.models import Segment, Transcript, Word
from clipforge.qa import QAReport

SAMPLE_RATE = 16000
Logger = Callable[[str, float | None], None]


@dataclass(frozen=True)
class WhisperConfig:
    model: str
    device: str
    compute_type: str
    chunk_seconds: float  # 0 = single pass
    overlap_seconds: float
    reason: str = ""


def resolve_config(settings, env_report: dict[str, Any] | None) -> WhisperConfig:
    """Explicit settings win; "auto" uses what clipforge-check found on this machine."""
    derived = (env_report or {}).get("defaults", {}).get("whisper") or {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "reason": "no env report yet (run clipforge-check); using the safe CPU default",
    }

    def pick(value: str, key: str) -> str:
        return derived[key] if value == "auto" else value

    device = pick(settings.whisper_device, "device")
    # GPUs handle a full episode in one pass; on CPU, chunk so no single call runs for hours
    # and a crash loses one chunk, not the episode.
    chunk = settings.whisper_chunk_minutes * 60 if device == "cpu" else 0.0
    return WhisperConfig(
        model=pick(settings.whisper_model, "model"),
        device=device,
        compute_type=pick(settings.whisper_compute_type, "compute_type"),
        chunk_seconds=chunk,
        overlap_seconds=settings.whisper_chunk_overlap_seconds,
        reason=(
            derived.get("reason", "")
            if "auto"
            in (settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
            else "set in .env"
        ),
    )


def plan_chunks(duration: float, chunk_s: float, overlap_s: float) -> list[tuple[float, float]]:
    """[start, end) windows covering the audio; neighbours overlap by `overlap_s`."""
    if chunk_s <= 0 or duration <= chunk_s:
        return [(0.0, duration)]
    if overlap_s >= chunk_s:
        raise ValueError("overlap must be shorter than the chunk")
    chunks = []
    start = 0.0
    while True:
        end = min(start + chunk_s, duration)
        chunks.append((start, end))
        if end >= duration:
            return chunks
        start = end - overlap_s


def merge_chunks(chunks: list[tuple[float, float, list[Segment]]]) -> list[Segment]:
    """Join per-chunk segments (already in episode time) without duplicates or gaps.

    Each overlap is split at its midpoint: a word belongs to the chunk whose side of the
    midpoint its centre falls on. Every word is kept exactly once.
    """
    merged: list[Segment] = []
    for i, (c_start, c_end, segs) in enumerate(chunks):
        lo = -1e18 if i == 0 else (c_start + chunks[i - 1][1]) / 2
        hi = 1e18 if i == len(chunks) - 1 else (chunks[i + 1][0] + c_end) / 2
        for seg in segs:
            words = [w for w in seg.words if lo <= (w.start + w.end) / 2 < hi]
            if not words:
                continue
            merged.append(
                Segment(
                    start=words[0].start,
                    end=words[-1].end,
                    text=" ".join(w.text for w in words),
                    words=words,
                )
            )
    merged.sort(key=lambda s: s.start)
    return merged


class _WhisperModel(Protocol):
    def transcribe(self, audio: np.ndarray, **kw: Any) -> tuple[Iterable[Any], Any]: ...


def _segments_from_whisper(raw: Iterable[Any], offset: float) -> list[Segment]:
    out: list[Segment] = []
    for s in raw:
        words = [
            Word(
                text=w.word.strip(),
                start=round(w.start + offset, 3),
                end=round(w.end + offset, 3),
                probability=round(float(w.probability), 3),
            )
            for w in (s.words or [])
            if w.word.strip()
        ]
        if words:
            out.append(
                Segment(
                    start=words[0].start,
                    end=words[-1].end,
                    text=" ".join(w.text for w in words),
                    words=words,
                )
            )
    return out


def load_whisper(cfg: WhisperConfig) -> _WhisperModel:
    from faster_whisper import WhisperModel

    return WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)


def transcribe(
    audio: np.ndarray,
    cfg: WhisperConfig,
    log: Logger,
    model: _WhisperModel | None = None,
    language: str | None = None,
) -> Transcript:
    duration = len(audio) / SAMPLE_RATE
    log(f"Loading whisper {cfg.model} on {cfg.device} ({cfg.compute_type})", None)
    model = model or load_whisper(cfg)
    chunks = plan_chunks(duration, cfg.chunk_seconds, cfg.overlap_seconds)
    results: list[tuple[float, float, list[Segment]]] = []
    detected = language
    for i, (start, end) in enumerate(chunks):
        piece = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
        raw, info = model.transcribe(
            piece,
            language=detected,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        segs = _segments_from_whisper(raw, offset=start)
        detected = detected or getattr(info, "language", None)
        results.append((start, end, segs))
        log(
            f"Transcribed {end / 60:.1f} of {duration / 60:.1f} min (chunk {i + 1}/{len(chunks)})",
            (i + 1) / len(chunks),
        )
    return Transcript(
        language=detected,
        duration=round(duration, 3),
        model=cfg.model,
        device=cfg.device,
        segments=merge_chunks(results),
    )


def transcribe_file(wav: Path, cfg: WhisperConfig, log: Logger) -> Transcript:
    from clipforge.core.ingest import load_wav

    audio, sr = load_wav(wav)
    if sr != SAMPLE_RATE:
        raise ValueError(f"expected {SAMPLE_RATE} Hz audio, got {sr}")
    return transcribe(audio, cfg, log)


def transcript_qa(t: Transcript) -> QAReport:
    qa = QAReport(step="transcribe")
    words = t.words
    if t.fake:
        qa.check(
            "real transcription",
            False,
            "FAKE_MODELS stub transcript: plumbing test only, not a real result",
        )
    qa.check("words found", len(words) > 0, f"{len(words)} words, language {t.language}")
    if not words:
        return qa
    qa.check("every word has start < end", all(w.start < w.end for w in words))
    order_ok = all(a.start <= b.start for a, b in zip(words, words[1:], strict=False))
    qa.check("words are in time order", order_ok)
    inside = all(0 <= w.start and w.end <= t.duration + 0.05 for w in words)
    qa.check("all words inside the episode", inside, f"duration {t.duration:.1f} s")
    dupes = [
        a.start
        for a, b in zip(words, words[1:], strict=False)
        if a.text == b.text and abs(a.start - b.start) < 0.05
    ]
    qa.check(
        "no duplicated words at chunk joins",
        not dupes,
        f"{len(dupes)} duplicates",
        at_seconds=dupes[0] if dupes else None,
    )
    speech = sum(w.end - w.start for w in words)
    qa.check(
        "speech covers a reasonable share of the episode",
        speech / max(t.duration, 1e-6) > 0.2,
        f"{speech / max(t.duration, 1e-6):.0%} of the episode has words",
        severity="warn",
    )
    low = [w for w in words if w.probability < 0.4]
    qa.check(
        "word confidence",
        len(low) / len(words) < 0.15,
        f"{len(low)} low-confidence words ({len(low) / len(words):.0%}); noisy audio makes word "
        "times unreliable, Phase 2 cuts by RMS",
        severity="warn",
    )
    return qa


# ---- test stub ------------------------------------------------------------------------------

_FAKE_LINES = [
    ("Welcome back to the show, today my guest is Marcus Hale.", "?"),
    ("Thanks for having me, it's great to be here.", ""),
    ("So Marcus, what really happened the night before the final?", "?"),
    ("Honestly, nobody knows this, but I almost walked away from the whole thing.", ""),
    ("I had a two million dollar offer on the table and I said no in five minutes.", ""),
    ("Wait, you turned down two million dollars?", "?"),
    ("I did, because my coach told me one sentence that changed everything.", ""),
    ("He said, you can buy a house with that money, but you can't buy this moment.", ""),
    ("And the next day we won by a single point.", ""),
    ("That is unbelievable, what did your family say?", "?"),
    ("My mother didn't talk to me for a week, she thought I was crazy.", ""),
    ("Then she saw the trophy and she cried for an hour.", ""),
]


def fake_transcript(duration: float) -> Transcript:
    """Deterministic stand-in used only when FAKE_MODELS=true (pipeline plumbing tests)."""
    segments: list[Segment] = []
    t = 0.5
    i = 0
    while t < duration - 1:
        text, _ = _FAKE_LINES[i % len(_FAKE_LINES)]
        words = []
        for token in text.split():
            dur = 0.18 + 0.03 * len(token)
            if t + dur > duration - 0.2:
                break
            words.append(Word(text=token, start=round(t, 3), end=round(t + dur, 3)))
            t += dur + 0.06
        if words:
            segments.append(
                Segment(
                    start=words[0].start,
                    end=words[-1].end,
                    text=" ".join(w.text for w in words),
                    words=words,
                )
            )
        t += 0.6
        i += 1
    return Transcript(
        language="en", duration=duration, model="fake", device="none", segments=segments, fake=True
    )
