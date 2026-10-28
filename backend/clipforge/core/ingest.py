"""Ingest: rights gate, yt-dlp download, ffprobe, audio extraction.

Spec rule: the full episode is downloaded as ONE muxed file by ONE yt-dlp call. We never
download sections or fetch audio and video separately and join them later (that drifts).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from clipforge.models import MediaInfo
from clipforge.qa import QAReport

# Audio and video streams of a good muxed file end within this many seconds of each other.
AV_DURATION_TOLERANCE = 0.15

Logger = Callable[[str], None]


class RightsNotConfirmed(Exception):
    pass


class IngestError(Exception):
    pass


def require_rights(confirmed: bool) -> None:
    if not confirmed:
        raise RightsNotConfirmed(
            "Confirm you have the rights or permission to clip this content before ingesting."
        )


_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def validate_url(url: str) -> str:
    url = url.strip()
    if not _URL_RE.match(url):
        raise IngestError(f"Not a valid http(s) URL: {url!r}")
    return url


def ytdlp_command(url: str, dest_dir: Path) -> list[str]:
    """One yt-dlp call that writes one muxed MP4 of the whole episode."""
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-part",
        "--restrict-filenames",
        # Best video up to 1080p + best audio, merged by yt-dlp into one file in this same
        # call; fall back to the best pre-muxed format.
        "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format",
        "mp4",
        "--newline",
        "-o",
        str(dest_dir / "source.%(ext)s"),
        url,
    ]


def download(url: str, dest_dir: Path, log: Logger) -> Path:
    validate_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = ytdlp_command(url, dest_dir)
    log("Downloading with yt-dlp (one muxed file)")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8"
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-20:]
        if "[download]" in line and "%" in line and not line.endswith("ETA Unknown"):
            # Throttle: only log every ~10%.
            m = re.search(r"(\d+(?:\.\d+)?)%", line)
            if m and float(m.group(1)) % 10 < 0.5:
                log(line)
        elif line.startswith(("[Merger]", "ERROR")):
            log(line)
    if proc.wait() != 0:
        raise IngestError("yt-dlp failed:\n" + "\n".join(tail))
    files = [p for p in dest_dir.glob("source.*") if p.suffix not in (".part", ".ytdl")]
    if len(files) != 1:
        raise IngestError(f"Expected one downloaded file, found {[p.name for p in files]}")
    return files[0]


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise IngestError("ffprobe not found on PATH. Install ffmpeg.")
    return exe


def _fps(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    num, _, den = rate.partition("/")
    try:
        return round(float(num) / float(den or 1), 3)
    except (ValueError, ZeroDivisionError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> MediaInfo:
    out = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise IngestError(f"ffprobe couldn't read {path.name}: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    # Ignore cover-art "video" streams in audio files.
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _float(data.get("format", {}).get("duration")) or 0.0
    return MediaInfo(
        path=str(path),
        duration=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        video_duration=_float(video.get("duration")) if video else None,
        audio_duration=_float(audio.get("duration")) if audio else None,
        fps=_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=audio.get("channels") if audio else None,
    )


def ingest_qa(info: MediaInfo) -> QAReport:
    qa = QAReport(step="ingest")
    qa.check("file is readable", info.duration > 0, f"{info.duration:.1f} s")
    qa.check("has an audio stream", info.has_audio, info.audio_codec or "none")
    qa.check(
        "has a video stream",
        info.has_video,
        f"{info.width}x{info.height} {info.video_codec}" if info.has_video else "audio only",
        # Audio-only episodes can still be transcribed and picked; framing needs video later.
        severity="warn",
    )
    if info.has_video:
        qa.check("frame rate detected", info.fps is not None, f"{info.fps} fps")
    if info.video_duration and info.audio_duration:
        diff = abs(info.video_duration - info.audio_duration)
        qa.check(
            "audio and video lengths match (one muxed file, no drift)",
            diff <= AV_DURATION_TOLERANCE,
            f"video {info.video_duration:.3f} s, audio {info.audio_duration:.3f} s, "
            f"diff {diff:.3f} s",
        )
    return qa


def extract_audio(src: Path, dest: Path, sample_rate: int = 16000) -> Path:
    """Mono PCM WAV for transcription and diarization."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise IngestError("ffmpeg not found on PATH.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.wav")
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise IngestError(f"ffmpeg audio extraction failed: {proc.stderr.strip()}")
    tmp.replace(dest)  # atomic: a crashed run never leaves a half-written cache file
    return dest


def load_wav(path: Path):
    """Read 16-bit mono WAV into float32 numpy [-1, 1]."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise IngestError(f"{path.name} must be 16-bit mono")
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0, sr
