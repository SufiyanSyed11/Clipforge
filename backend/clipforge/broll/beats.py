"""Split a clip into B-roll beats: one new visual every 2-3 s, each beat 62 % B-roll then
back to the speaker. The hook (first sentence, at least 1.2 s) stays on the speaker, and
beats never overlap the host-reaction insert."""

from __future__ import annotations

import re

from clipforge.editspec import BrollSlot, EditSpec

BEAT_MIN, BEAT_MAX = 2.0, 3.0
BROLL_SHARE = 0.62
HOOK_MIN = 1.2
END_KEEP = 1.0  # the last second (punchline) stays on the speaker


def plan_slots(spec: EditSpec) -> list[BrollSlot]:
    words = spec.captions
    if not words:
        return []
    fps = spec.fps
    # end of the hook: first sentence end, at least HOOK_MIN s in
    hook_end = HOOK_MIN
    for w in words:
        if w.out_end >= HOOK_MIN and re.search(r"[.?!]$", w.spoken):
            hook_end = w.out_end
            break
    else:
        hook_end = max(HOOK_MIN, words[0].out_end)
    limit = spec.duration - END_KEEP
    blocked = []
    if spec.host_insert and spec.host_insert_enabled:
        blocked.append((spec.host_insert.out_start, spec.host_insert.out_end))
    slots: list[BrollSlot] = []
    starts = [w.out_start for w in words if w.out_start >= hook_end]
    if not starts:
        return []
    t = starts[0]
    while t + BEAT_MIN <= limit:
        # end the beat on a word boundary between 2 and 3 s later
        ends = [w.out_start for w in words if t + BEAT_MIN <= w.out_start <= t + BEAT_MAX]
        beat_end = ends[-1] if ends else min(t + BEAT_MAX, limit)
        b0 = round(t * fps) / fps
        b1 = round((t + (beat_end - t) * BROLL_SHARE) * fps) / fps
        if not any(a < b1 and b0 < b for a, b in blocked) and b1 - b0 >= 0.8:
            text = " ".join(w.spoken for w in words if b0 - 0.05 <= w.out_start < beat_end)
            slots.append(BrollSlot(index=len(slots), out_start=b0, out_end=b1, beat_text=text))
        t = beat_end
    return slots
