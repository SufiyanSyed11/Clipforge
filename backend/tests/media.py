"""Generate small media fixtures with ffmpeg (no real podcast content)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def make_video(
    path: Path, seconds: float = 30.0, fps: int = 30, audio_short_by: float = 0.0
) -> Path:
    """Test pattern + tone. `audio_short_by` > 0 makes the audio stream shorter than the video
    (what a badly joined audio/video download looks like)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    audio_seconds = seconds - audio_short_by
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={audio_seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return path


def make_audio(path: Path, seconds: float = 10.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=44100:duration={seconds}",
        str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return path
