"""EditSpec: a clip described as data. Rendering is a pure function of it.

Times are in seconds. `src_*` is episode (source) time; `out_*` is time in the finished clip.
Every cut time is on the output frame grid (1/fps) and inside measured silence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Piece(BaseModel):
    """A kept window of the episode, played in order."""

    src_start: float
    src_end: float

    @property
    def duration(self) -> float:
        return self.src_end - self.src_start


class Cut(BaseModel):
    """Where and why one edge of a piece was cut."""

    edge: Literal["start", "end"]
    src_time: float
    requested: float  # time the model/user asked for (whisper-based)
    reason: Literal["clip_start", "clip_end", "seam", "pause", "profanity_skip", "trim"]
    silence_from: float  # the measured silent run that contains the cut
    silence_to: float
    min_rms_db: float
    threshold_db: float
    extended_by: float = 0.0  # seconds added because no real gap was near the requested time


class Join(BaseModel):
    """A point in the output where two pieces meet."""

    out_time: float
    src_before: float
    src_after: float
    reason: str


class Bleep(BaseModel):
    src_start: float
    src_end: float
    word: str


class CaptionWord(BaseModel):
    text: str  # what is shown (corrected/censored)
    spoken: str  # what the transcript says
    out_start: float
    out_end: float
    src_start: float
    src_end: float
    emphasis: bool = False
    censored: bool = False
    speaker: str | None = None


class CameraKey(BaseModel):
    """Crop centre and zoom for the main footage at an output time (Phase 3)."""

    out_time: float
    cx: float  # crop centre x in source pixels
    cy: float
    crop_w: float  # crop width in source pixels before zoom
    zoom: float = 1.0
    cut: bool = False  # True = hard reposition (new shot/speaker), no smoothing across it


class HostInsert(BaseModel):
    out_start: float
    out_end: float
    src_start: float
    src_end: float
    cx: float
    cy: float
    crop_w: float
    shot_id: str
    reason: str


class BrollChoice(BaseModel):
    source: str  # pexels | pixabay | youtube | library | manual
    source_id: str
    title: str
    page_url: str = ""
    license: str = ""
    path: str  # local cached file
    src_start: float
    width: int
    height: int
    contact_sheet: str = ""
    verdict: dict = Field(default_factory=dict)
    rules_hash: str = ""
    approved_by: Literal["vision", "owner", ""] = ""
    flags: list[str] = Field(default_factory=list)


class BrollSlot(BaseModel):
    index: int
    out_start: float
    out_end: float
    beat_text: str
    queries: list[str] = Field(default_factory=list)
    named_person: str | None = None
    status: Literal["empty", "filled", "weak"] = "empty"
    weak_reason: str = ""
    choice: BrollChoice | None = None
    candidates: list[BrollChoice] = Field(default_factory=list)


class MixSpec(BaseModel):
    voice_lufs: float = -14.0
    bgm_path: str | None = None
    bgm_db_under_voice: float = 19.0
    limiter_db: float = -1.0
    duck: bool = True


class EditSpec(BaseModel):
    version: int = 1
    clip_id: int | None = None
    source_path: str
    source_fps: float | None = None
    fps: int = 60
    width: int = 1080
    height: int = 1920
    frame_rate_mode: Literal["repeat", "interpolate"] = "repeat"
    pieces: list[Piece]
    cuts: list[Cut] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    bleeps: list[Bleep] = Field(default_factory=list)
    removed: list[dict] = Field(default_factory=list)  # e.g. sentences skipped for profanity
    # The words the clip is meant to contain (what the picker/trim asked for, minus skipped
    # and bleeped words). The gate checks every one of them is heard in the cut audio.
    intended_words: list[str] = Field(default_factory=list)
    captions: list[CaptionWord] = Field(default_factory=list)
    camera: list[CameraKey] = Field(default_factory=list)
    host_insert: HostInsert | None = None
    host_insert_enabled: bool = True
    broll: list[BrollSlot] = Field(default_factory=list)
    mix: MixSpec = Field(default_factory=MixSpec)
    grade: str = "clean"
    logo_path: str | None = None

    @property
    def duration(self) -> float:
        return round(sum(p.duration for p in self.pieces), 6)

    @property
    def frame_count(self) -> int:
        return round(self.duration * self.fps)

    def out_offsets(self) -> list[float]:
        offs, t = [], 0.0
        for p in self.pieces:
            offs.append(t)
            t += p.duration
        return offs

    def src_to_out(self, src: float) -> float | None:
        for off, p in zip(self.out_offsets(), self.pieces, strict=True):
            if p.src_start - 1e-6 <= src <= p.src_end + 1e-6:
                return off + (src - p.src_start)
        return None

    def out_to_src(self, out: float) -> float:
        for off, p in zip(self.out_offsets(), self.pieces, strict=True):
            if out < off + p.duration - 1e-9:
                return p.src_start + max(0.0, out - off)
        last = self.pieces[-1]
        return last.src_end
