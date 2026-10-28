"""Download/cache candidates, find cut-free windows, make contact sheets, perceptual hashes."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np

from clipforge.broll.sources import Candidate

MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
CUT_DIFF = 28.0  # mean grey change between samples (0-255) that counts as a scene cut
SHEET_FRAMES = 6


class MediaError(Exception):
    pass


def fetch(c: Candidate, cache: Path, transport: httpx.BaseTransport | None = None) -> Path:
    """Local file for a candidate (downloaded once into the cache)."""
    if c.local_path:
        return Path(c.local_path)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{c.key}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".part")
    if c.source == "youtube":
        proc = subprocess.run(
            [
                sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-part",
                "-f", "bv*[height>=1080]+ba/b[height>=1080]/bv*+ba/b",
                "--merge-output-format", "mp4", "-o", str(tmp), c.download_url or c.page_url,
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )  # fmt: skip
        if proc.returncode != 0 or not tmp.exists():
            raise MediaError(f"yt-dlp failed for {c.page_url}: {proc.stderr[-300:]}")
    else:
        size = 0
        with httpx.Client(timeout=60.0, transport=transport, follow_redirects=True) as client:
            with client.stream("GET", c.download_url) as r:
                r.raise_for_status()
                with tmp.open("wb") as out:
                    for chunk in r.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            out.close()
                            tmp.unlink(missing_ok=True)
                            raise MediaError(
                                f"{c.key} is larger than {MAX_DOWNLOAD_BYTES >> 20} MB"
                            )
                        out.write(chunk)
    tmp.replace(dest)
    return dest


def sample_frames(path: Path, start: float, seconds: float, fps: float, width: int = 320):
    from clipforge.core.render import video_size

    sw, sh = video_size(str(path))
    h = int(round(sh * width / sw / 2) * 2)
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{seconds:.3f}",
            "-an", "-vf", f"fps={fps},scale={width}:{h}",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ],
        capture_output=True,
        check=False,
    )  # fmt: skip
    n = len(proc.stdout) // (width * h * 3)
    return [
        np.frombuffer(proc.stdout[i * width * h * 3 : (i + 1) * width * h * 3], np.uint8).reshape(
            h, width, 3
        )
        for i in range(n)
    ]


def cut_free_window(path: Path, duration: float, need: float) -> float | None:
    """Start of a window of `need` seconds with no scene cut, near the middle of the longest
    cut-free stretch. None if there is none."""
    fps = 4.0
    frames = sample_frames(path, 0.0, duration, fps, width=160)
    if len(frames) < 2:
        return None
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
    cuts = [0.0]
    for k in range(1, len(grays)):
        if float(np.abs(grays[k] - grays[k - 1]).mean()) > CUT_DIFF:
            cuts.append(k / fps)
    cuts.append(len(frames) / fps)
    best = None
    for a, b in zip(cuts, cuts[1:], strict=False):
        a2, b2 = a + 0.3, b - 0.3  # stay clear of the cut itself
        if b2 - a2 >= need and (best is None or b2 - a2 > best[1] - best[0]):
            best = (a2, b2)
    if best is None:
        return None
    mid = (best[0] + best[1]) / 2
    return round(max(best[0], min(mid - need / 2, best[1] - need)), 3)


def contact_sheet(
    path: Path, start: float, need: float, out: Path
) -> tuple[Path, list[np.ndarray]]:
    """6 frames across the window in a 3x2 grid (what the vision check and the owner see)."""
    frames = sample_frames(path, start, need, SHEET_FRAMES / max(need, 0.5), width=320)
    if not frames:
        raise MediaError(f"No frames in {path.name} at {start:.2f} s")
    idx = np.linspace(0, len(frames) - 1, SHEET_FRAMES).round().astype(int)
    picked = [frames[i] for i in idx]
    rows = [np.hstack(picked[i : i + 3]) for i in (0, 3)]
    sheet = np.vstack(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return out, picked


def dhash(frame: np.ndarray) -> int:
    g = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (9, 8), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def file_sha1(path: Path, limit: int = 64 * 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        h.update(f.read(limit))
    return h.hexdigest()


def podcast_hashes(media: Path, duration: float) -> list[int]:
    """Perceptual hashes of the podcast at 0.5 fps: B-roll matching any of them is rejected."""
    frames = sample_frames(media, 0.0, duration, 0.5, width=160)
    return [dhash(f) for f in frames]


def looks_like_podcast(frames: list[np.ndarray], podcast: list[int], max_dist: int = 6) -> bool:
    for f in frames:
        h = dhash(f)
        if any(hamming(h, p) <= max_dist for p in podcast):
            return True
    return False
