"""Verification gate. A clip can't be exported unless every check passes; failures carry the
exact output timestamp (and source time where useful).

Checks (CLAUDE.md, "Verification gate"):
- re-transcribe the cut voice track and diff it against the intended words;
- RMS 40-60 ms before and after every join must be silence;
- first 0.3 s: natural onset; last 0.45 s: natural decay;
- rendered file: audio duration == video duration, frame count == expected.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import numpy as np

from clipforge.core.audio import Levels, rms_db, rms_trace
from clipforge.editspec import EditSpec
from clipforge.qa import QAReport

SR = 48000
# A word counts as transcribed if the re-transcription has it in order; whisper itself
# mishears a few words, so a small share may differ, but never at a join or an edge.
MIN_WORD_MATCH = 0.9
EDGE_ZONE_S = 0.6  # a missing word this close to a join/edge counts as a clipped word

Transcriber = Callable[[np.ndarray, int], list[str]]


def norm_word(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower()).strip("'")


def _clip_levels(voice: np.ndarray) -> Levels:
    trace = rms_trace(voice, SR)
    return Levels(float(np.percentile(trace, 10)), float(np.percentile(trace, 90)))


def verify_voice(
    voice: np.ndarray,
    spec: EditSpec,
    transcriber: Transcriber | None,
    *,
    transcriber_is_real: bool = True,
) -> QAReport:
    qa = QAReport(step="verify")
    levels = _clip_levels(voice)
    thr = levels.threshold_db
    dur = len(voice) / SR

    # 1. joins: 40-60 ms either side must be silent.
    for j in spec.joins:
        before = rms_db(voice, SR, j.out_time - 0.060, j.out_time - 0.040)
        after = rms_db(voice, SR, j.out_time + 0.040, j.out_time + 0.060)
        qa.check(
            f"join at {j.out_time:.3f} s is in silence ({j.reason})",
            before < thr and after < thr,
            f"40-60 ms before {before:.1f} dB, after {after:.1f} dB, threshold {thr:.1f} dB "
            f"(source {j.src_before:.3f} -> {j.src_after:.3f} s)",
            at_seconds=j.out_time,
        )

    # 2. onset and decay.
    head = rms_db(voice, SR, 0.0, 0.03)
    qa.check(
        "first 0.3 s: starts in silence (no clipped first word)",
        head < thr,
        f"first 30 ms at {head:.1f} dB (threshold {thr:.1f})",
        at_seconds=0.0,
    )
    starts = rms_trace(voice[: int(0.3 * SR)], SR)
    qa.check(
        "first 0.3 s: speech begins (hook lands immediately)",
        bool((starts[3:] >= thr).any()),
        "speech energy found in the first 0.3 s" if (starts[3:] >= thr).any() else "silent",
        severity="warn",
        at_seconds=0.0,
    )
    tail = rms_db(voice, SR, dur - 0.05, dur)
    qa.check(
        "last 0.45 s: last word decays into silence",
        tail < thr,
        f"last 50 ms at {tail:.1f} dB (threshold {thr:.1f})",
        at_seconds=round(dur - 0.05, 3),
    )

    # 3. re-transcription diff against the words the clip is meant to contain.
    captions = [c for c in spec.captions if not c.censored]
    if spec.intended_words:
        intended_text = spec.intended_words
    else:
        intended_text = [c.spoken for c in captions]
    # Approximate output times for reporting: from captions where the word lines up.
    cap_times = {}
    for c in captions:
        cap_times.setdefault(norm_word(c.spoken), []).append(c.out_start)
    if transcriber is None:
        qa.check(
            "re-transcription matches the intended words",
            False,
            "no transcription model available, so clipped words can't be ruled out",
            at_seconds=0.0,
        )
        return qa
    heard = [norm_word(w) for w in transcriber(voice, SR) if norm_word(w)]
    want = [norm_word(w) for w in intended_text]
    sm = difflib.SequenceMatcher(a=want, b=heard, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    ratio = matched / max(len(want), 1)
    missing: list[tuple[str, float]] = []
    n_want = max(len(want), 1)
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for k in range(i1, i2):
                times = cap_times.get(want[k])
                # No caption time: estimate from the word's position in the clip.
                t = (
                    min(times, key=lambda x: abs(x - dur * k / n_want))
                    if times
                    else dur * k / n_want
                )
                missing.append((intended_text[k], round(t, 3)))
    extra = [
        heard[j]
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes()
        if tag == "insert"
        for j in range(j1, j2)
    ]
    edges = [0.0, dur] + [j.out_time for j in spec.joins]
    at_edges = [(w, t) for w, t in missing if min(abs(t - e) for e in edges) <= EDGE_ZONE_S]
    if not transcriber_is_real:
        qa.check(
            "re-transcription by a real model",
            False,
            "TEST DOUBLE transcriber: plumbing check only, not a real verification",
        )
    qa.check(
        "re-transcription matches the intended words",
        ratio >= MIN_WORD_MATCH,
        f"{matched}/{len(want)} words matched ({ratio:.0%})"
        + (f"; differs: {', '.join(f'{w!r}@{t:.2f}s' for w, t in missing[:8])}" if missing else ""),
        at_seconds=missing[0][1] if missing else None,
    )
    qa.check(
        "no word missing at a join or edge (zero clipped words)",
        not at_edges,
        "; ".join(f"{w!r} at {t:.2f} s" for w, t in at_edges) or "none",
        at_seconds=at_edges[0][1] if at_edges else None,
    )
    if extra:
        qa.check(
            "extra words beyond what was asked for",
            True,
            f"{len(extra)} extra (cut moved to a natural pause): {' '.join(extra[:8])}",
            severity="warn",
        )
    return qa


def probe_streams(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH")
    out = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_packets",
            "-show_entries",
            "stream=codec_type,duration,nb_read_packets,r_frame_rate,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def verify_render(path: Path, spec: EditSpec) -> QAReport:
    qa = QAReport(step="render-verify")
    info = probe_streams(path)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v or not a:
        qa.check("rendered file has audio and video", False, str(info))
        return qa
    frames = int(v.get("nb_read_packets") or 0)
    expected = spec.frame_count
    qa.check(
        "frame count matches",
        frames == expected,
        f"{frames} frames, expected {expected} ({spec.duration:.3f} s at {spec.fps} fps)",
    )
    vd, ad = float(v.get("duration") or 0), float(a.get("duration") or 0)
    # AAC works in 1024-sample frames; allow one of those (21 ms) plus a video frame.
    tol = 1024 / SR + 1 / spec.fps
    qa.check(
        "audio duration equals video duration",
        abs(vd - ad) <= tol,
        f"video {vd:.3f} s, audio {ad:.3f} s (tolerance {tol * 1000:.0f} ms)",
    )
    qa.check(
        "resolution",
        (int(v.get("width", 0)), int(v.get("height", 0))) == (spec.width, spec.height),
        f"{v.get('width')}x{v.get('height')}",
    )
    return qa


def whisper_transcriber(cfg) -> Transcriber:
    """Production transcriber: faster-whisper on the cut voice track."""
    from scipy.signal import resample_poly

    from clipforge.core.transcribe import load_whisper

    model = load_whisper(cfg)

    def run(voice: np.ndarray, sr: int) -> list[str]:
        audio16 = resample_poly(voice, 1, 3).astype(np.float32) if sr == 48000 else voice
        segs, _ = model.transcribe(
            audio16, word_timestamps=True, vad_filter=False, condition_on_previous_text=False
        )
        return [w.word.strip() for s in segs for w in (s.words or []) if w.word.strip()]

    return run
