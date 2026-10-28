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
    # Added to descriptions/captions only if set (the spec: no credit line unless added).
    credit_line: str = ""
    logo_path: str | None = None
    # Owner decision 3: alternative seam first, bleep only as a last resort.
    profanity_mode: Literal["prefer_alternative_then_bleep", "bleep", "allow"] = (
        "prefer_alternative_then_bleep"
    )
    # Owner decision 4: frame repetition unless interpolation is explicitly enabled.
    frame_rate_mode: Literal["repeat", "interpolate"] = "repeat"
    # One consistent colour grade for every clip.
    grade: Literal["none", "clean", "warm", "cool"] = "clean"
    caption_font: str = "Arial Black"
    caption_uppercase: bool = True
    caption_accent: str = "#FFD400"  # emphasis colour for key words
    # Royalty-free music bed (uploaded in Settings); None = no music.
    bgm_path: str | None = None
    # SFX only on motivated transitions (B-roll cutaways); off = clean cuts, no SFX.
    sfx_path: str | None = None
    sfx_on_transitions: bool = False
    # Download SFace (38 MB) once to recognise the host across camera angles.
    face_recognition: bool = True
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
    if cfg.style.width * 16 != cfg.style.height * 9 or cfg.style.width % 2:
        raise ValueError("Output must be vertical 9:16 with even dimensions (e.g. 1080x1920)")
    if cfg.style.fps not in (30, 60):
        raise ValueError("fps must be 30 or 60")
    # Uploaded files only: settings can't point the renderer at arbitrary files on disk.
    from pathlib import Path

    from clipforge.settings import get_settings

    branding = (get_settings().data_dir / "branding").resolve()
    for field in ("logo_path", "bgm_path", "sfx_path"):
        value = getattr(cfg, field)
        if value and branding not in Path(value).resolve().parents:
            raise ValueError(f"{field} must be a file uploaded in Settings")
    with session_scope() as s:
        row = s.get(AppSetting, _KEY)
        if row:
            row.value = cfg.model_dump()
        else:
            s.add(AppSetting(key=_KEY, value=cfg.model_dump()))
    return cfg
