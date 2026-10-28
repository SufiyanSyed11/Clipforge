"""Learning loop: what worked on the channel recently, fed into the picker and the copy writer.

Input: recent uploads with their stats (from the YouTube APIs, or a CSV exported from YouTube
Studio / vidIQ). Each video gets a performance score (views per hour since publishing, weighted
by average percentage viewed). The top half is compared with the bottom half on length, title
patterns and topic words. The result is a short text block for prompts and a structured
summary for the "what worked" panel and the "why this clip" notes.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field

from clipforge.core.captions import STOPWORDS
from clipforge.core.copywriter import emojis

FRESH_DAYS = 7  # insights older than this aren't used in prompts


class VideoStat(BaseModel):
    title: str
    published_at: datetime | None = None
    duration_s: float | None = None
    views: float = 0
    avg_view_pct: float | None = None  # 0-100
    avg_view_s: float | None = None
    likes: float | None = None
    comments: float | None = None
    video_id: str | None = None


class Insights(BaseModel):
    updated_at: datetime
    source: str
    window_days: int
    videos: int
    best_length: str | None = None
    title_patterns: dict[str, float] = Field(default_factory=dict)  # pattern -> lift
    top_topics: list[str] = Field(default_factory=list)
    best_hours: list[int] = Field(default_factory=list)  # UTC hours with the best results
    top_titles: list[str] = Field(default_factory=list)
    summary: str = ""


def title_features(title: str) -> dict[str, bool]:
    return {
        "one emoji": len(emojis(title)) == 1,
        "quoted punchline": bool(re.search(r"[\"“”']{1}[^\"“”']{6,}[\"“”']", title)),
        "question": "?" in title,
        "number or money": bool(re.search(r"\d|\$|million|billion", title, re.I)),
        "named person": bool(re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", title)),
        "short title (<= 60 chars)": len(title) <= 60,
    }


def length_bucket(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 20:
        return "under 20 s"
    if seconds <= 25:
        return "20-25 s"
    if seconds <= 30:
        return "25-30 s"
    return "over 30 s"


def score(v: VideoStat, now: datetime) -> float:
    hours = 24.0
    if v.published_at:
        pub = v.published_at if v.published_at.tzinfo else v.published_at.replace(tzinfo=UTC)
        hours = max(1.0, (now - pub).total_seconds() / 3600)
    retention = (v.avg_view_pct or 50.0) / 100.0
    return v.views / hours * (0.5 + retention)


def _words(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z][a-zA-Z']{3,}", title.lower()) if w not in STOPWORDS]


def analyse(videos: list[VideoStat], source: str, window_days: int) -> Insights:
    now = datetime.now(UTC)
    ranked = sorted(videos, key=lambda v: score(v, now), reverse=True)
    ins = Insights(updated_at=now, source=source, window_days=window_days, videos=len(ranked))
    if len(ranked) < 2:
        ins.summary = (
            f"Only {len(ranked)} upload(s) in the last {window_days} days: "
            "not enough to learn from."
        )
        return ins
    half = max(1, len(ranked) // 2)
    top, bottom = ranked[:half], ranked[half:]

    def share(group: list[VideoStat], key: str) -> float:
        return sum(title_features(v.title)[key] for v in group) / len(group)

    for key in title_features("x"):
        lift = share(top, key) - share(bottom, key)
        if abs(lift) >= 0.25:
            ins.title_patterns[key] = round(lift, 2)
    buckets: dict[str, list[float]] = {}
    for v in ranked:
        b = length_bucket(v.duration_s)
        if b:
            buckets.setdefault(b, []).append(score(v, now))
    if buckets:
        ins.best_length = max(buckets, key=lambda b: sum(buckets[b]) / len(buckets[b]))
    top_words: dict[str, int] = {}
    for v in top:
        for w in set(_words(v.title)):
            top_words[w] = top_words.get(w, 0) + 1
    bottom_words = {w for v in bottom for w in _words(v.title)}
    ins.top_topics = [
        w for w, n in sorted(top_words.items(), key=lambda kv: -kv[1]) if w not in bottom_words
    ][:6]
    hours: dict[int, list[float]] = {}
    # Date-only exports land at midnight: no time of day to learn from.
    timed = [v for v in ranked if v.published_at and v.published_at.time() != time(0)]
    for v in timed:
        hours.setdefault(v.published_at.hour, []).append(score(v, now))
    ins.best_hours = sorted(hours, key=lambda h: -sum(hours[h]) / len(hours[h]))[:2]
    ins.top_titles = [v.title for v in top[:3]]
    parts = [f"Last {window_days} days, {len(ranked)} uploads ({source})."]
    if ins.best_length:
        parts.append(f"Best length: {ins.best_length}.")
    good = [k for k, lift in ins.title_patterns.items() if lift > 0]
    bad = [k for k, lift in ins.title_patterns.items() if lift < 0]
    if good:
        parts.append("Top titles more often had: " + ", ".join(good) + ".")
    if bad:
        parts.append("Weaker titles more often had: " + ", ".join(bad) + ".")
    if ins.top_topics:
        parts.append("Topics in the best performers: " + ", ".join(ins.top_topics) + ".")
    if ins.top_titles:
        parts.append("Best titles: " + " | ".join(ins.top_titles) + ".")
    ins.summary = " ".join(parts)
    return ins


def why_this_clip(clip_plan: dict, duration: float, ins: Insights | None) -> list[str]:
    """Data-backed notes for the clip card, when insights exist."""
    if ins is None or ins.videos < 2:
        return []
    notes = []
    b = length_bucket(duration)
    if ins.best_length and b == ins.best_length:
        notes.append(f"length {b} matches your best-performing recent shorts")
    titles = clip_plan.get("title_options", [])
    for key, lift in ins.title_patterns.items():
        if lift > 0 and any(title_features(t)[key] for t in titles):
            notes.append(f"a title option uses '{key}', common in your top recent titles")
    words = set(_words(" ".join([clip_plan.get("hook", ""), clip_plan.get("payoff", "")])))
    hits = [t for t in ins.top_topics if t in words]
    if hits:
        notes.append("topic overlap with recent winners: " + ", ".join(hits))
    return notes


# ---- CSV import (YouTube Studio / vidIQ exports) ----------------------------------------------

_COLS = {
    "title": ["video title", "title", "content", "video"],
    "views": ["views", "view count"],
    "avg_view_pct": ["average percentage viewed (%)", "average percentage viewed", "avg % viewed",
                     "average view percentage", "retention"],
    "avg_view_s": ["average view duration", "avg view duration", "avg. view duration"],
    "duration_s": ["duration", "length", "video duration"],
    "published_at": ["video publish time", "published", "publish date", "upload date", "date"],
    "likes": ["likes"],
    "comments": ["comments", "comments added"],
}  # fmt: skip


def _seconds(v: str) -> float | None:
    v = v.strip()
    if not v:
        return None
    if ":" in v:
        parts = [float(p) for p in v.split(":")]
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total
    try:
        return float(v)
    except ValueError:
        return None


def _num(v: str) -> float | None:
    v = v.replace(",", "").replace("%", "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def _date(v: str) -> datetime | None:
    v = v.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y"):  # fmt: skip
        try:
            d = datetime.strptime(v, fmt)
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_csv(text: str) -> list[VideoStat]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValueError("The CSV has no header row")
    lower = {h.strip().lower(): h for h in reader.fieldnames}
    cols: dict[str, str] = {}
    for key, names in _COLS.items():
        for n in names:
            if n in lower:
                cols[key] = lower[n]
                break
    if "title" not in cols or "views" not in cols:
        raise ValueError(
            "The CSV needs at least a title and a views column (YouTube Studio or vidIQ export)"
        )
    out = []
    for row in reader:
        title = (row.get(cols["title"]) or "").strip()
        if not title or title.lower() == "total":
            continue
        out.append(
            VideoStat(
                title=title,
                views=_num(row.get(cols["views"], "")) or 0,
                avg_view_pct=_num(row.get(cols.get("avg_view_pct", ""), "") or ""),
                avg_view_s=_seconds(row.get(cols.get("avg_view_s", ""), "") or ""),
                duration_s=_seconds(row.get(cols.get("duration_s", ""), "") or ""),
                published_at=_date(row.get(cols.get("published_at", ""), "") or ""),
                likes=_num(row.get(cols.get("likes", ""), "") or ""),
                comments=_num(row.get(cols.get("comments", ""), "") or ""),
            )
        )
    return out


def recent(videos: list[VideoStat], days: int) -> list[VideoStat]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    dated = [v for v in videos if v.published_at]
    if not dated:
        return videos  # CSV without dates: use what was exported
    return [v for v in dated if v.published_at >= cutoff]


# ---- storage ----------------------------------------------------------------------------------

_KEY = "learning_insights"


def save_insights(ins: Insights) -> None:
    from clipforge.db import AppSetting, session_scope

    with session_scope() as s:
        row = s.get(AppSetting, _KEY)
        data = ins.model_dump(mode="json")
        if row:
            row.value = data
        else:
            s.add(AppSetting(key=_KEY, value=data))


def load_insights() -> Insights | None:
    from clipforge.db import AppSetting, session_scope

    with session_scope() as s:
        row = s.get(AppSetting, _KEY)
        return Insights.model_validate(row.value) if row else None


def learning_context() -> str:
    """Prompt block for the picker/copy writer; empty when there's nothing fresh."""
    ins = load_insights()
    if ins is None or ins.videos < 2:
        return ""
    updated = ins.updated_at if ins.updated_at.tzinfo else ins.updated_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - updated > timedelta(days=FRESH_DAYS):
        return ""
    return "Channel performance (use as a tie-breaker, never over the clip rules):\n" + ins.summary


def insights_dict(ins: Insights | None) -> dict[str, Any] | None:
    return ins.model_dump(mode="json") if ins else None
