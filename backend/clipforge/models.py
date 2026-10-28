"""Data models shared by the pipeline steps."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MediaInfo(BaseModel):
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    video_duration: float | None = None
    audio_duration: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None


class Word(BaseModel):
    text: str
    start: float
    end: float
    probability: float = 1.0
    speaker: str | None = None


class Segment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str | None = None
    duration: float
    model: str
    device: str
    segments: list[Segment]
    # True when produced by the test stub (FAKE_MODELS); never shown as a real result.
    fake: bool = False

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.end > start and w.start < end]


class SpeakerStats(BaseModel):
    speaker: str
    talk_seconds: float
    talk_share: float
    turns: int
    questions: int
    first_spoke_at: float
    sample: str = ""


class HostSuggestion(BaseModel):
    speaker: str
    confidence: Literal["high", "medium", "low"]
    reasons: list[str]


# ---- clip picker output -------------------------------------------------------------------


class ClipSegment(BaseModel):
    start: float
    end: float
    text: str


class RiskFlag(BaseModel):
    type: Literal["profanity", "unverifiable_claim", "private_info", "defamation", "other"]
    detail: str
    at_seconds: float | None = None


class ClipPlan(BaseModel):
    segments: list[ClipSegment] = Field(min_length=1)
    hook: str
    context: str
    payoff: str
    virality_score: int = Field(ge=1, le=10)
    reasons: list[str] = Field(min_length=3, max_length=3)
    title_options: list[str] = Field(min_length=3, max_length=3)
    broll_beats: list[str]
    risk_flags: list[RiskFlag]
    improvements: str

    @property
    def duration(self) -> float:
        return sum(s.end - s.start for s in self.segments)

    @property
    def start(self) -> float:
        return min(s.start for s in self.segments)


class PickedClip(BaseModel):
    """A clip after the hard rules ran: ranked, with code-side flags."""

    rank: int
    plan: ClipPlan
    duration: float
    stitched: bool
    flags: list[str] = Field(default_factory=list)
    # Phase 1 windows come from whisper timestamps; Phase 2 moves every cut to real silence.
    windows_are_approximate: bool = True
