"""Audio measurement: 10 ms RMS traces, noise floors, silent runs, loudness.

Levels are in dBFS (0 dB = full scale). For reference, the spec's "noise floor 100-400" is in
16-bit units: 100/32768 ≈ -50 dBFS, 400/32768 ≈ -38 dBFS.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HOP = 0.010  # 10 ms RMS frames
SR = 48000


def rms_trace(audio: np.ndarray, sr: int = SR, hop: float = HOP) -> np.ndarray:
    """RMS per non-overlapping `hop` window, in dBFS. Frame i covers [i*hop, (i+1)*hop)."""
    n = int(round(sr * hop))
    frames = len(audio) // n
    if frames == 0:
        return np.full(1, -120.0)
    x = audio[: frames * n].astype(np.float64).reshape(frames, n)
    rms = np.sqrt((x * x).mean(axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def rms_db(audio: np.ndarray, sr: int, t0: float, t1: float) -> float:
    a = audio[max(0, int(t0 * sr)) : max(0, int(t1 * sr))]
    if len(a) == 0:
        return -120.0
    return float(20 * np.log10(max(float(np.sqrt(np.mean(a.astype(np.float64) ** 2))), 1e-6)))


@dataclass(frozen=True)
class Levels:
    floor_db: float
    speech_db: float

    @property
    def threshold_db(self) -> float:
        """Silence threshold: a dip must get well below speech, relative to the local floor.

        Clean audio (floor -70, speech -20) -> -55 dB. Noisy audio (floor -40, speech -18)
        -> about -33 dB: judged by dips relative to the floor, not by an absolute level.
        """
        span = max(self.speech_db - self.floor_db, 0.0)
        return self.floor_db + max(4.0, 0.3 * span)


def local_levels(trace: np.ndarray, center: int, radius: int = 500) -> Levels:
    """Floor (10th percentile) and speech level (90th) within ±radius frames (±5 s)."""
    lo, hi = max(0, center - radius), min(len(trace), center + radius)
    window = trace[lo:hi] if hi > lo else trace
    return Levels(float(np.percentile(window, 10)), float(np.percentile(window, 90)))


def silent_runs(
    trace: np.ndarray, threshold_db: float, lo: int, hi: int, min_frames: int
) -> list[tuple[int, int]]:
    """[start, end) frame ranges inside [lo, hi) where every frame is below threshold."""
    lo, hi = max(0, lo), min(len(trace), hi)
    runs: list[tuple[int, int]] = []
    i = lo
    while i < hi:
        if trace[i] < threshold_db:
            j = i
            while j < hi and trace[j] < threshold_db:
                j += 1
            if j - i >= min_frames:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def snap(t: float, fps: float) -> float:
    """Frame-grid snapping from the spec: snap(t) = round(t * FPS) / FPS."""
    return round(t * fps) / fps


def grid_points(t0: float, t1: float, fps: float) -> list[float]:
    """Frame-grid times inside [t0, t1]."""
    first = int(np.ceil(t0 * fps - 1e-9))
    last = int(np.floor(t1 * fps + 1e-9))
    return [k / fps for k in range(first, last + 1)]


def load_wav48(path: Path) -> np.ndarray:
    from clipforge.core.ingest import load_wav

    audio, sr = load_wav(path)
    if sr != SR:
        raise ValueError(f"{path.name}: expected {SR} Hz, got {sr}")
    return audio


def write_wav(path: Path, audio: np.ndarray, sr: int = SR) -> Path:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)
    return path


def measure_loudness(path: Path) -> dict[str, float]:
    """Integrated loudness (LUFS), loudness range and true peak (dBTP) via ffmpeg loudnorm."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        raise RuntimeError(f"loudness measurement failed: {proc.stderr[-400:]}")
    d = json.loads(m.group(0))

    def num(key: str) -> float:
        v = d.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("-inf")

    return {
        "lufs": num("input_i"),
        "true_peak": num("input_tp"),
        "lra": num("input_lra"),
        "threshold": num("input_thresh"),
    }
