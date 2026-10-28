"""Face analysis for framing: shots, face tracks, mouth movement, who is who.

- Detection: YuNet (bundled). Recognition (same person across shots): SFace, downloaded once
  and checksum-verified; without it, identities fall back to the diarized speaker per track.
- The episode is sampled at 5 fps, 640 px wide. Per sample: faces, a mouth-region difference
  against the same track's previous sample (lip movement), a global frame difference
  (stillness) and a colour-histogram shot-change test.
- Faces much smaller than the biggest face in the frame (TV walls, background screens) are
  marked rejected and never framed.
- Each track is matched to a diarized speaker by how its mouth movement follows that speaker's
  speech; tracks are grouped into identities by face embedding.
"""

from __future__ import annotations

import hashlib
import subprocess
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from clipforge.models import Transcript

SAMPLE_FPS = 5.0
ANALYSIS_WIDTH = 640
SMALL_FACE_RATIO = 0.35  # faces below this share of the biggest face's height are rejected
SHOT_HIST_CORR = 0.55
SHOT_DIFF = 40.0  # mean grey-level change (0-255) that counts as a cut
SFACE_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
SAME_PERSON_COSINE = 0.40  # SFace's suggested threshold is 0.363


@dataclass
class Face:
    box: tuple[float, float, float, float]  # x, y, w, h in analysis-frame pixels
    score: float
    landmarks: np.ndarray | None = None  # 5x2: eyes, nose, mouth corners
    embedding: np.ndarray | None = None


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> list[Face]: ...
    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray | None: ...


def _yunet_path() -> str:
    return str(resources.files("clipforge.assets").joinpath("face_detection_yunet_2023mar.onnx"))


def ensure_sface(models_dir: Path) -> Path | None:
    """Download SFace once (38 MB) and verify its checksum. None if unavailable."""
    path = models_dir / "face_recognition_sface_2021dec.onnx"
    if path.exists() and _sha256(path) == SFACE_SHA256:
        return path
    models_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    try:
        urllib.request.urlretrieve(SFACE_URL, tmp)  # noqa: S310 - fixed https URL
    except OSError:
        tmp.unlink(missing_ok=True)
        return None
    if _sha256(tmp) != SFACE_SHA256:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(path)
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class YuNetDetector:
    def __init__(self, sface_path: Path | None = None, score_threshold: float = 0.75) -> None:
        self.det = cv2.FaceDetectorYN.create(_yunet_path(), "", (320, 320), score_threshold)
        self.rec = cv2.FaceRecognizerSF.create(str(sface_path), "") if sface_path else None
        self._size: tuple[int, int] | None = None

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self.det.setInputSize((w, h))
            self._size = (w, h)
        _, faces = self.det.detect(frame)
        out = []
        for f in faces if faces is not None else []:
            out.append(
                Face(
                    box=(float(f[0]), float(f[1]), float(f[2]), float(f[3])),
                    score=float(f[14]),
                    landmarks=np.array(f[4:14], dtype=np.float32).reshape(5, 2),
                )
            )
        return out

    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray | None:
        if self.rec is None or face.landmarks is None:
            return None
        row = np.array([*face.box, *face.landmarks.reshape(-1), face.score], dtype=np.float32)
        aligned = self.rec.alignCrop(frame, row)
        return self.rec.feature(aligned).reshape(-1).astype(np.float32)


def decode_samples(
    path: str, sample_fps: float = SAMPLE_FPS, width: int = ANALYSIS_WIDTH
) -> Iterator[tuple[float, np.ndarray]]:
    """(time, BGR frame) at `sample_fps`, scaled to `width`."""
    from clipforge.core.render import video_size

    sw, sh = video_size(path)
    h = int(round(sh * width / sw / 2) * 2)
    proc = subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-i", path, "-an",
            "-vf", f"fps={sample_fps},scale={width}:{h}",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    size = width * h * 3
    k = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield k / sample_fps, np.frombuffer(buf, np.uint8).reshape(h, width, 3)
            k += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return inter / max(aw * ah + bw * bh - inter, 1e-6)


def _mouth_patch(gray: np.ndarray, face: Face) -> np.ndarray | None:
    x, y, w, h = face.box
    if face.landmarks is not None:
        (lx, ly), (rx, ry) = face.landmarks[3], face.landmarks[4]
        cx, cy = (lx + rx) / 2, (ly + ry) / 2 + 0.05 * h
        mw = max(abs(rx - lx) * 1.6, 0.3 * w)
    else:
        cx, cy, mw = x + w / 2, y + 0.78 * h, 0.5 * w
    mh = mw * 0.6
    x0, y0 = int(max(0, cx - mw / 2)), int(max(0, cy - mh / 2))
    x1, y1 = int(min(gray.shape[1], cx + mw / 2)), int(min(gray.shape[0], cy + mh / 2))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    patch = cv2.resize(gray[y0:y1, x0:x1], (32, 16), interpolation=cv2.INTER_AREA)
    patch = patch.astype(np.float32)
    return (patch - patch.mean()) / (patch.std() + 1e-3)


@dataclass
class _Track:
    id: int
    shot: int
    box: tuple[float, float, float, float]
    patch: np.ndarray | None
    first: float
    last: float
    n: int = 0
    embs: list[np.ndarray] = field(default_factory=list)
    last_emb_t: float = -1e9


def analyze_samples(
    samples: Iterable[tuple[float, np.ndarray]],
    detector: Detector,
    *,
    scale_to_source: float = 1.0,
    log: Callable[[str], None] = lambda m: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    out_samples: list[dict] = []
    tracks: dict[int, _Track] = {}
    active: list[_Track] = []
    prev_hist = None
    prev_small = None
    shot = 0
    next_id = 0
    for k, (t, frame) in enumerate(samples):
        if k % 50 == 0 and cancelled():
            raise InterruptedError("analysis cancelled")
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32)
        diff = float(np.abs(small - prev_small).mean()) if prev_small is not None else 0.0
        if (
            # A camera switch changes the colour histogram or the overall picture sharply.
            prev_hist is not None
            and (
                cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < SHOT_HIST_CORR
                or diff > SHOT_DIFF
            )
        ):
            shot += 1
            active = []
        prev_hist, prev_small = hist, small

        faces = detector.detect(frame)
        max_h = max((f.box[3] for f in faces), default=0.0)
        sample_faces = []
        used: set[int] = set()
        for f in sorted(faces, key=lambda f: -f.box[3]):
            match = None
            best = 0.3
            for tr in active:
                if tr.id in used:
                    continue
                iou = _iou(tr.box, f.box)
                if iou > best:
                    best, match = iou, tr
            patch = _mouth_patch(gray, f)
            mouth = 0.0
            if match is None:
                match = _Track(next_id, shot, f.box, patch, t, t)
                tracks[next_id] = match
                active.append(match)
                next_id += 1
            else:
                if match.patch is not None and patch is not None:
                    mouth = float(np.abs(patch - match.patch).mean())
                match.box, match.patch, match.last = f.box, patch, t
            used.add(match.id)
            match.n += 1
            if t - match.last_emb_t >= 2.0:
                emb = detector.embed(frame, f)
                if emb is not None:
                    match.embs.append(emb / (np.linalg.norm(emb) + 1e-6))
                    match.last_emb_t = t
            small_face = max_h > 0 and f.box[3] < SMALL_FACE_RATIO * max_h
            s = scale_to_source
            sample_faces.append(
                {
                    "track": match.id,
                    "box": [round(v * s, 1) for v in f.box],
                    "score": round(f.score, 3),
                    "mouth": round(mouth, 4),
                    "rejected": "small face (TV wall/background)" if small_face else None,
                }
            )
        active = [tr for tr in active if t - tr.last <= 1.0]
        out_samples.append(
            {"t": round(t, 3), "shot": shot, "diff": round(diff, 3), "faces": sample_faces}
        )
        if k and k % 1500 == 0:
            log(f"Face analysis: {t / 60:.1f} min")
    track_info = {
        str(tr.id): {
            "shot": tr.shot,
            "first": tr.first,
            "last": tr.last,
            "n": tr.n,
            "emb": (np.mean(tr.embs, axis=0) / (np.linalg.norm(np.mean(tr.embs, axis=0)) + 1e-6))
            .round(5)
            .tolist()
            if tr.embs
            else None,
        }
        for tr in tracks.values()
    }
    return {
        "sample_fps": SAMPLE_FPS,
        "samples": out_samples,
        "tracks": track_info,
        "shots": shot + 1,
    }


def speaker_at(transcript: Transcript, t: float) -> str | None:
    for seg in transcript.segments:
        if seg.start - 0.15 <= t <= seg.end + 0.15:
            for w in seg.words:
                if w.start - 0.1 <= t <= w.end + 0.1:
                    return w.speaker or seg.speaker
    return None


def identify(analysis: dict[str, Any], transcript: Transcript, host_speaker: str | None) -> dict:
    """Match tracks to speakers by mouth movement, group them into identities."""
    samples = analysis["samples"]
    times = [s["t"] for s in samples]
    speaking = [speaker_at(transcript, t) for t in times]
    speakers = sorted({sp for sp in speaking if sp})
    per_track: dict[str, list[tuple[float, str | None, bool]]] = {}
    for s, sp in zip(samples, speaking, strict=True):
        for f in s["faces"]:
            per_track.setdefault(str(f["track"]), []).append((f["mouth"], sp, bool(f["rejected"])))
    track_speaker: dict[str, dict] = {}
    for tid, rows in per_track.items():
        mouths = np.array([r[0] for r in rows])
        rejected = sum(r[2] for r in rows) / len(rows) > 0.7
        best, best_score = None, 0.0
        for sp in speakers:
            on = np.array([r[1] == sp for r in rows])
            if on.sum() < 3 or (~on).sum() < 3:
                continue
            score = (mouths[on].mean() - mouths[~on].mean()) / (mouths.std() + 1e-6)
            if score > best_score:
                best, best_score = sp, float(score)
        track_speaker[tid] = {
            "speaker": best if best_score >= 0.3 else None,
            "score": round(best_score, 3),
            "rejected": rejected,
        }
    # identities: embeddings when available, else the matched speaker
    tracks = analysis["tracks"]
    identities: dict[str, str] = {}
    centroids: list[tuple[str, np.ndarray]] = []
    for tid, info in sorted(tracks.items(), key=lambda kv: -kv[1]["n"]):
        emb = info.get("emb")
        ident = None
        if emb is not None:
            v = np.array(emb, dtype=np.float32)
            for name, c in centroids:
                if float(np.dot(v, c)) >= SAME_PERSON_COSINE:
                    ident = name
                    break
            if ident is None:
                ident = f"P{len(centroids) + 1}"
                centroids.append((ident, v))
        else:
            sp = track_speaker.get(tid, {}).get("speaker")
            ident = f"speaker:{sp}" if sp else f"track:{tid}"
        identities[tid] = ident
    ident_speaker: dict[str, str] = {}
    votes: dict[str, dict[str, float]] = {}
    for tid, ident in identities.items():
        ts = track_speaker.get(tid)
        if ts and ts["speaker"]:
            votes.setdefault(ident, {}).setdefault(ts["speaker"], 0.0)
            votes[ident][ts["speaker"]] += ts["score"] * tracks[tid]["n"]
    for ident, v in votes.items():
        ident_speaker[ident] = max(v, key=v.get)
    host_ident = None
    if host_speaker:
        cands = [i for i, sp in ident_speaker.items() if sp == host_speaker]
        if cands:
            host_ident = max(cands, key=lambda i: votes[i][host_speaker])
    return {
        "track_speaker": track_speaker,
        "identities": identities,
        "identity_speaker": ident_speaker,
        "host_identity": host_ident,
        "used_embeddings": bool(centroids),
    }


def sample_index(analysis: dict[str, Any], t: float) -> int:
    k = int(round(t * analysis["sample_fps"]))
    return max(0, min(len(analysis["samples"]) - 1, k))
