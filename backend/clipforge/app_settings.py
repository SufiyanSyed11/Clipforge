"""Owner-editable settings with the spec's defaults (docs/SPEC.md, ARCHITECTURE.md section 0)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from clipforge.db import AppSetting, session_scope


class StylePreset(BaseModel):
    name: str = "Default (pro editor)"
    width: int = 1080
    height: int = 1920
    fps: int = 60
    min_clip_seconds: float = 18.0
    max_clip_seconds: float = 30.0  # hard max; the API refuses anything above 30
    captions: str = "one-word, centered, bold, high contrast, key-word emphasis"
    logo_position: str = "top-right"
    push_in_zoom: bool = True
    transitions: str = "clean cut unless motivated"
    default_clip_count: int = 8


class AppConfig(BaseModel):
    style: StylePreset = Field(default_factory=StylePreset)
    brand_tag: str = "#DoubleCoverage"
    logo_path: str | None = None
    # Owner decision 3: alternative seam first, bleep only as a last resort.
    profanity_mode: Literal["prefer_alternative_then_bleep", "bleep", "allow"] = (
        "prefer_alternative_then_bleep"
    )
    # Owner decision 4: frame repetition unless interpolation is explicitly enabled.
    frame_rate_mode: Literal["repeat", "interpolate"] = "repeat"
    # Owner decision 7: spec defaults, editable; enforced on every B-roll pick (Phase 4).
    broll_banned_content: list[str] = Field(
        default_factory=lambda: ["women", "any person who is not clearly identified"]
    )
    broll_reject_rules: list[str] = Field(
        default_factory=lambda: [
            "on-screen text",
            "captions",
            "watermarks",
            "logos",
            "network score bugs",
            "lower-thirds",
            "black bars",
            "wrong person",
            "wrong event",
            "soft or low-res footage (< 1080p)",
            "scene cuts inside the window",
            "the podcast footage itself",
        ]
    )


_KEY = "app_config"


def load_config() -> AppConfig:
    with session_scope() as s:
        row = s.get(AppSetting, _KEY)
        return AppConfig.model_validate(row.value) if row else AppConfig()


def save_config(cfg: AppConfig) -> AppConfig:
    if cfg.style.max_clip_seconds > 30:
        raise ValueError("max_clip_seconds can't exceed the 30 s hard max")
    if cfg.style.min_clip_seconds > cfg.style.max_clip_seconds:
        raise ValueError("min_clip_seconds must be <= max_clip_seconds")
    with session_scope() as s:
        row = s.get(AppSetting, _KEY)
        if row:
            row.value = cfg.model_dump()
        else:
            s.add(AppSetting(key=_KEY, value=cfg.model_dump()))
    return cfg
