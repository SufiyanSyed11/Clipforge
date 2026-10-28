"""Speech-recognition TEST DOUBLE for synthetic speech (FAKE_MODELS only).

It "hears" the frequency-coded tone bursts from `synth`: timing from the audio, text from the
script. It cannot understand real speech and always marks its output as fake.
"""

from __future__ import annotations

import numpy as np

from clipforge.models import Segment, Transcript, Word
from clipforge.testing import synth

WIN = 2048
HOP = 160


def _bursts(audio: np.ndarray, sr: int) -> list[tuple[float, float, int]]:
    """(start, end, frequency index) of each tone burst, in time order."""
    if len(audio) < WIN:
        return []
    frames = 1 + (len(audio) - WIN) // HOP
    out: list[tuple[float, float, int]] = []
    freqs = np.fft.rfftfreq(WIN, 1 / sr)
    bins = [int(np.argmin(np.abs(freqs - synth.freq(j)))) for j in range(synth.CYCLE)]
    win = np.hanning(WIN).astype(np.float32)
    # Process in blocks to bound memory on long audio.
    block = 4000
    mags = []
    for b0 in range(0, frames, block):
        b1 = min(frames, b0 + block)
        idx = np.arange(WIN)[None, :] + HOP * np.arange(b0, b1)[:, None]
        spec = np.abs(np.fft.rfft(audio[idx] * win, axis=1))
        mags.append(spec[:, bins])
    m = np.concatenate(mags)  # frames x CYCLE
    level = 0.25 * WIN / 4 * 0.2  # 20% of a full burst's peak magnitude
    best = m.argmax(axis=1)
    on = m.max(axis=1) > level
    k = 0
    while k < frames:
        if not on[k]:
            k += 1
            continue
        j = best[k]
        e = k
        while e < frames and on[e] and best[e] == j:
            e += 1
        start = (k * HOP + WIN / 2) / sr
        end = (e * HOP + WIN / 2) / sr
        if end - start > 0.08:
            out.append((start, end, int(j)))
        k = e
    return out


def transcribe(audio: np.ndarray, sr: int, duration: float) -> Transcript:
    words: list[Word] = []
    for start, end, j in _bursts(audio, sr):
        words.append(Word(text=synth.SCRIPT[j], start=round(start, 3), end=round(end, 3)))
    segments: list[Segment] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        if w.text.endswith((".", "?", "!")):
            segments.append(
                Segment(
                    start=cur[0].start,
                    end=cur[-1].end,
                    text=" ".join(x.text for x in cur),
                    words=cur,
                )
            )
            cur = []
    if cur:
        segments.append(
            Segment(
                start=cur[0].start, end=cur[-1].end, text=" ".join(x.text for x in cur), words=cur
            )
        )
    return Transcript(
        language="en",
        duration=duration,
        model="fake-asr",
        device="none",
        segments=segments,
        fake=True,
    )


def heard_words(voice: np.ndarray, sr: int) -> list[str]:
    """Verification re-transcription double: words whose burst is (nearly) whole."""
    out = []
    for start, end, j in _bursts(voice, sr):
        text = synth.SCRIPT[j]
        full = 0.22 + 0.02 * min(len(text), 8)
        if end - start >= 0.85 * full:
            out.append(text)
    return out
