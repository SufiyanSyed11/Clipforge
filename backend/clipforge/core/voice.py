"""Assemble the clip's voice track from an EditSpec: sample-exact pieces, tiny fades at every
join and edge, bleeps (mute + 1 kHz tone) for profanity that couldn't be avoided."""

from __future__ import annotations

import numpy as np

from clipforge.editspec import EditSpec

SR = 48000
FADE_S = 0.008  # tiny fades at joins: inaudible, but no clicks
BLEEP_HZ = 1000.0
BLEEP_RAMP_S = 0.005


def _ramp(n: int) -> np.ndarray:
    return 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n, dtype=np.float32))


def assemble_voice(audio48: np.ndarray, spec: EditSpec, sr: int = SR) -> np.ndarray:
    parts: list[np.ndarray] = []
    fade = max(1, int(FADE_S * sr))
    for p in spec.pieces:
        a = int(round(p.src_start * sr))
        n = int(round(p.duration * sr))  # exact: piece edges are on the 1/fps grid
        piece = np.zeros(n, dtype=np.float32)
        chunk = audio48[a : a + n]
        piece[: len(chunk)] = chunk
        for b in spec.bleeps:
            s0 = max(b.src_start, p.src_start)
            s1 = min(b.src_end, p.src_end)
            if s1 <= s0:
                continue
            i0, i1 = int(round((s0 - p.src_start) * sr)), int(round((s1 - p.src_start) * sr))
            seg = piece[i0:i1]
            level = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) if len(seg) else 0.05
            amp = min(0.5, max(0.05, level * np.sqrt(2)))
            t = np.arange(i1 - i0, dtype=np.float32) / sr
            tone = (amp * np.sin(2 * np.pi * BLEEP_HZ * t)).astype(np.float32)
            r = min(int(BLEEP_RAMP_S * sr), len(tone) // 2)
            if r > 0:
                tone[:r] *= _ramp(r)
                tone[-r:] *= _ramp(r)[::-1]
            piece[i0:i1] = tone  # speech fully muted under the tone
        f = min(fade, n // 2)
        if f > 0:
            piece[:f] *= _ramp(f)
            piece[-f:] *= _ramp(f)[::-1]
        parts.append(piece)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
