"""Renderer: EditSpec -> 1080x1920 60 fps MP4. A pure function of the spec and its inputs.

Every output frame is built here: the main footage through the camera path (crop + slow
push-in), B-roll slots, the host-reaction insert. Frames go to one ffmpeg encode that adds the
captions (ASS), the logo and the colour grade, with the mixed audio. Source video is decoded
per window at 60 fps by frame repetition (owner decision 4); motion interpolation only when
`frame_rate_mode == "interpolate"`.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from clipforge.editspec import CameraKey, EditSpec

GRADES = {
    "none": "",
    "clean": "eq=contrast=1.04:saturation=1.06:gamma=0.98",
    "warm": "eq=contrast=1.05:saturation=1.08,colorbalance=rs=0.03:bs=-0.03",
    "cool": "eq=contrast=1.05:saturation=1.02,colorbalance=rs=-0.02:bs=0.03",
}


class RenderError(Exception):
    pass


class RenderCancelled(Exception):
    pass


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RenderError("ffmpeg not found on PATH")
    return exe


@functools.lru_cache(maxsize=64)
def video_size(path: str) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    out = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        s = json.loads(out.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except (KeyError, IndexError, ValueError) as e:
        raise RenderError(f"Can't read video size of {Path(path).name}") from e


@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True if h264_nvenc can actually encode here (driver + GPU present)."""
    try:
        proc = subprocess.run(
            [
                _ffmpeg(),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=black:s=256x256:d=0.1",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired, RenderError):
        return False


def encoder_args(encoder: str) -> list[str]:
    if encoder == "auto":
        encoder = "h264_nvenc" if nvenc_available() else "libx264"
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


# ---- timeline -------------------------------------------------------------------------------


@dataclass
class VSeg:
    out_start: float
    out_end: float
    kind: str  # main | broll | host
    path: str
    src_start: float
    crop: tuple[float, float, float] | None = None  # (cx, cy, crop_w) for host inserts


def build_timeline(spec: EditSpec) -> list[VSeg]:
    """Ordered video segments covering [0, duration] exactly."""
    overlays: list[VSeg] = []
    for slot in spec.broll:
        if slot.status == "filled" and slot.choice:
            overlays.append(
                VSeg(slot.out_start, slot.out_end, "broll", slot.choice.path, slot.choice.src_start)
            )
    hi = spec.host_insert
    if hi and spec.host_insert_enabled:
        overlays.append(
            VSeg(
                hi.out_start,
                hi.out_end,
                "host",
                spec.source_path,
                hi.src_start,
                (hi.cx, hi.cy, hi.crop_w),
            )
        )
    overlays.sort(key=lambda s: s.out_start)
    for a, b in zip(overlays, overlays[1:], strict=False):
        if b.out_start < a.out_end - 1e-6:
            raise RenderError(f"Overlapping cutaways at {b.out_start:.2f} s")

    segs: list[VSeg] = []
    for off, p in zip(spec.out_offsets(), spec.pieces, strict=True):
        start, end = off, off + p.duration
        t = start
        for ov in overlays:
            if ov.out_end <= t or ov.out_start >= end:
                continue
            if ov.out_start > t:
                segs.append(VSeg(t, ov.out_start, "main", spec.source_path, p.src_start + t - off))
            t = max(t, min(ov.out_end, end))
        if t < end - 1e-9:
            segs.append(VSeg(t, end, "main", spec.source_path, p.src_start + t - off))
    for ov in overlays:
        segs.append(ov)
    segs.sort(key=lambda s: s.out_start)
    # Overlays replace main video; drop main pieces they fully cover.
    out: list[VSeg] = []
    for s in segs:
        if out and s.kind == "main" and s.out_start < out[-1].out_end - 1e-9:
            continue
        out.append(s)
    return out


# ---- framing ----------------------------------------------------------------------------------


def base_crop_w(sw: int, sh: int, out_w: int, out_h: int) -> float:
    """Widest crop with the output aspect that fits the source."""
    return min(float(sw), sh * out_w / out_h)


def camera_at(keys: list[CameraKey], t: float, default: CameraKey) -> CameraKey:
    if not keys:
        return default
    if t <= keys[0].out_time:
        return keys[0]
    for a, b in zip(keys, keys[1:], strict=False):
        if a.out_time <= t < b.out_time:
            if b.cut:
                return a  # hard reposition happens exactly at b
            u = (t - a.out_time) / max(b.out_time - a.out_time, 1e-9)
            return CameraKey(
                out_time=t,
                cx=a.cx + (b.cx - a.cx) * u,
                cy=a.cy + (b.cy - a.cy) * u,
                crop_w=a.crop_w + (b.crop_w - a.crop_w) * u,
                zoom=a.zoom + (b.zoom - a.zoom) * u,
            )
    return keys[-1]


def crop_frame(
    frame: np.ndarray, cx: float, cy: float, crop_w: float, zoom: float, out_w: int, out_h: int
) -> np.ndarray:
    sh, sw = frame.shape[:2]
    w = min(crop_w / max(zoom, 1e-6), float(sw))
    h = w * out_h / out_w
    if h > sh:
        h = float(sh)
        w = h * out_w / out_h
    x0 = int(round(min(max(cx - w / 2, 0), sw - w)))
    y0 = int(round(min(max(cy - h / 2, 0), sh - h)))
    crop = frame[y0 : y0 + int(round(h)), x0 : x0 + int(round(w))]
    interp = cv2.INTER_AREA if crop.shape[1] > out_w else cv2.INTER_CUBIC
    return cv2.resize(crop, (out_w, out_h), interpolation=interp)


def cover_frame(frame: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    sh, sw = frame.shape[:2]
    cw = base_crop_w(sw, sh, out_w, out_h)
    return crop_frame(frame, sw / 2, sh / 2, cw, 1.0, out_w, out_h)


# ---- decode / encode ------------------------------------------------------------------------


class _Decoder:
    def __init__(self, path: str, start: float, seconds: float, interpolate: bool) -> None:
        self.w, self.h = video_size(path)
        vf = "fps=60" if not interpolate else "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc"
        self.proc = subprocess.Popen(
            [
                _ffmpeg(),
                "-v",
                "error",
                "-ss",
                f"{max(0.0, start):.4f}",
                "-i",
                path,
                "-t",
                f"{seconds + 0.25:.4f}",
                "-an",
                "-vf",
                vf,
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.size = self.w * self.h * 3
        self.last: np.ndarray | None = None

    def read(self) -> np.ndarray:
        assert self.proc.stdout is not None
        buf = self.proc.stdout.read(self.size)
        if len(buf) == self.size:
            self.last = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
        elif self.last is None:
            self.last = np.zeros((self.h, self.w, 3), np.uint8)
        return self.last  # past the end: hold the last frame (exact frame count)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


def render(
    spec: EditSpec,
    audio_wav: Path,
    out_path: Path,
    *,
    ass_path: Path | None = None,
    encoder: str = "auto",
    log: Callable[[str, float | None], None] = lambda m, p: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    out_w, out_h, fps = spec.width, spec.height, spec.fps
    total = spec.frame_count
    timeline = build_timeline(spec)
    sw, sh = (out_w, out_h) if spec.audio_only else video_size(spec.source_path)
    background = np.full((out_h, out_w, 3), (24, 18, 14), np.uint8)
    default_cam = CameraKey(
        out_time=0.0, cx=sw / 2, cy=sh / 2, crop_w=base_crop_w(sw, sh, out_w, out_h)
    )

    filters = []
    grade = GRADES.get(spec.grade, "")
    if grade:
        filters.append(grade)
    if ass_path:
        filters.append(f"ass={ass_path.name}")
    vchain = ",".join(filters) if filters else "null"
    inputs = [
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", str(fps),
        "-i", "pipe:0", "-i", str(audio_wav),
    ]  # fmt: skip
    if spec.logo_path and Path(spec.logo_path).exists():
        inputs += ["-i", str(spec.logo_path)]
        logo_w = int(out_w * 0.16)
        margin_x, margin_y = int(out_w * 0.05), int(out_h * 0.06)  # inside platform safe zones
        fc = (
            f"[0:v]{vchain}[v0];[2:v]scale={logo_w}:-1,format=rgba[lg];"
            f"[v0][lg]overlay=W-w-{margin_x}:{margin_y}:format=auto[v]"
        )
    else:
        fc = f"[0:v]{vchain}[v]"
    tmp = out_path.with_suffix(".part.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(), "-y", "-v", "error", *inputs,
        "-filter_complex", fc, "-map", "[v]", "-map", "1:a",
        *encoder_args(encoder),
        "-pix_fmt", "yuv420p", "-r", str(fps), "-frames:v", str(total),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(tmp),
    ]  # fmt: skip
    enc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ass_path.parent) if ass_path else None,  # ASS by bare name: no path escaping
    )
    written = 0
    try:
        for seg in timeline:
            f0, f1 = round(seg.out_start * fps), round(seg.out_end * fps)
            n = f1 - f0
            if n <= 0:
                continue
            plain = seg.kind == "main" and spec.audio_only
            dec = (
                None
                if plain
                else _Decoder(
                    seg.path, seg.src_start, n / fps, spec.frame_rate_mode == "interpolate"
                )
            )
            try:
                for k in range(n):
                    if dec is None:  # audio-only episode: plain background behind captions
                        assert enc.stdin is not None
                        enc.stdin.write(background.tobytes())
                        written += 1
                        continue
                    frame = dec.read()
                    t = (f0 + k) / fps
                    if seg.kind == "broll":
                        out = cover_frame(frame, out_w, out_h)
                    elif seg.kind == "host" and seg.crop:
                        cx, cy, cw = seg.crop
                        out = crop_frame(frame, cx, cy, cw, 1.0, out_w, out_h)
                    else:
                        cam = camera_at(spec.camera, t, default_cam)
                        out = crop_frame(frame, cam.cx, cam.cy, cam.crop_w, cam.zoom, out_w, out_h)
                    assert enc.stdin is not None
                    enc.stdin.write(np.ascontiguousarray(out).tobytes())
                    written += 1
                    if written % 30 == 0:
                        if cancelled():
                            raise RenderCancelled()
                        if written % 300 == 0:
                            log(f"Rendered {written}/{total} frames", written / total)
            finally:
                if dec is not None:
                    dec.close()
        assert enc.stdin is not None
        enc.stdin.close()
        err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
        if enc.wait() != 0:
            raise RenderError(f"Encoder failed: {err[-800:]}")
    except BaseException:
        if enc.poll() is None:
            enc.kill()
            enc.wait()
        tmp.unlink(missing_ok=True)
        raise
    if written != total:
        tmp.unlink(missing_ok=True)
        raise RenderError(f"Wrote {written} frames, expected {total}")
    tmp.replace(out_path)  # atomic: a half-written render never looks finished
    return out_path
