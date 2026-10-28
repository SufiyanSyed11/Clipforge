"""Posting helpers: a suggested time, a calendar entry and a per-platform checklist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clipforge.core.copywriter import CopyPack
from clipforge.core.learn import Insights

DEFAULT_LOCAL_HOUR = 18


def suggest_time(insights: Insights | None, now: datetime | None = None) -> tuple[datetime, str]:
    """Next slot at the channel's best recent hour (UTC), else 18:00 local, at least 1 h out."""
    now = now or datetime.now(UTC)
    earliest = now + timedelta(hours=1)
    if insights and insights.best_hours:
        hour = insights.best_hours[0]
        t = earliest.replace(hour=hour, minute=0, second=0, microsecond=0)
        if t < earliest:
            t += timedelta(days=1)
        return t, f"your best recent upload hour ({hour:02d}:00 UTC)"
    local = earliest.astimezone()
    t = local.replace(hour=DEFAULT_LOCAL_HOUR, minute=0, second=0, microsecond=0)
    if t < local:
        t += timedelta(days=1)
    return t.astimezone(UTC), f"default {DEFAULT_LOCAL_HOUR}:00 local (no channel data yet)"


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(title: str, when: datetime, description: str, uid: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    start = when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    end = (when + timedelta(minutes=15)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//ClipForge//Posting plan//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}@clipforge",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{_ics_text('Post: ' + title)}",
            f"DESCRIPTION:{_ics_text(description)}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


def checklist(pack: CopyPack, when: datetime, why: str) -> str:
    local = when.astimezone()
    return (
        f"# Posting plan\n\nSuggested time: {local:%A %d %B %Y, %H:%M} (your time) — {why}.\n\n"
        "## YouTube Shorts\n"
        f"- [ ] Upload `video.mp4`\n- [ ] Title: {pack.title}\n"
        "- [ ] Description: from `copy.md`\n- [ ] Check the first frame works as the thumbnail\n\n"
        "## Instagram Reels\n"
        f"- [ ] Upload `video.mp4`\n- [ ] Caption + tags: {' '.join(pack.instagram_tags)}\n"
        "- [ ] Cover frame chosen\n\n"
        "## TikTok\n- [ ] Upload `video.mp4`\n- [ ] Caption from `copy.md` (Instagram text)\n\n"
        "## X\n"
        f"- [ ] Post: {pack.x_post}\n- [ ] Attach `video.mp4`\n\n"
        "Before posting: watch it once with sound on and listen at every cut.\n"
    )
