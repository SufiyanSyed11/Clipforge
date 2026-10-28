"""Synthetic "speech" for cut-engine tests. TEST FIXTURE ONLY.

Each word is a tone burst with its own frequency (syllable-like envelope), separated by gaps
we control: short inter-word gaps (no real silence), sentence gaps, and long pauses, over a
noise floor we control. Whisper-like word times are generated with deliberate errors of up
to ±0.3 s, so the engine must find the real gaps from the audio.

`decode_words` is a test-double transcriber: it reports a word only if (almost) the whole of
its burst is present, which is exactly what "zero clipped words" means.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from clipforge.models import Segment, Transcript, Word

SR = 48000
BASE_HZ = 400.0
STEP_HZ = 61.0  # distinct frequency per word, several FFT bins apart


@dataclass
class Truth:
    text: str
    start: float
    end: float
    freq: float


def make_speech(
    words: list[tuple[str, float, float]],
    *,
    noise_rms: float = 0.0005,
    level: float = 0.25,
    whisper_error: float = 0.25,
    seed: int = 0,
    lead: float = 1.0,
    tail: float = 1.0,
    speakers: list[str] | None = None,
):
    """words: (text, duration, gap_after). Returns (audio, transcript, truths)."""
    rng = np.random.default_rng(seed)
    total = lead + sum(d + g for _, d, g in words) + tail
    audio = rng.standard_normal(int(total * SR)).astype(np.float32) * noise_rms
    truths: list[Truth] = []
    t = lead
    for i, (text, d, g) in enumerate(words):
        f = BASE_HZ + STEP_HZ * i
        n = int(d * SR)
        tt = np.arange(n) / SR
        env = np.minimum(1.0, np.minimum(tt / 0.012, (d - tt) / 0.012))  # 12 ms ramps
        env *= 0.75 + 0.25 * np.sin(2 * np.pi * 5 * tt) ** 2
        s = int(t * SR)
        audio[s : s + n] += (level * env * np.sin(2 * np.pi * f * tt)).astype(np.float32)
        truths.append(Truth(text, t, t + d, f))
        t += d + g
    # Whisper-like times: a slowly drifting offset (up to ±whisper_error) plus jitter, in
    # word order like real whisper output (words never swap places).
    segs: list[Segment] = []
    cur: list[Word] = []
    offset = float(rng.uniform(-whisper_error, whisper_error))
    prev_start = 0.0
    for i, tr in enumerate(truths):
        offset = float(
            np.clip(offset + rng.normal(0, whisper_error / 3), -whisper_error, whisper_error)
        )
        jitter = whisper_error / 4
        ws = max(prev_start + 0.02, tr.start + offset + float(rng.uniform(-jitter, jitter)))
        we = max(ws + 0.05, tr.end + offset + float(rng.uniform(-jitter, jitter)))
        prev_start = ws
        sp = speakers[i] if speakers else "S1"
        cur.append(Word(text=tr.text, start=round(ws, 3), end=round(we, 3), speaker=sp))
        if tr.text.endswith((".", "?", "!")) or i == len(truths) - 1:
            segs.append(
                Segment(
                    start=cur[0].start,
                    end=cur[-1].end,
                    text=" ".join(w.text for w in cur),
                    speaker=sp,
                    words=cur,
                )
            )
            cur = []
    tr_ = Transcript(duration=round(total, 3), model="synthetic", device="none", segments=segs)
    return audio, tr_, truths


def decode_words(truths: list[Truth], min_presence: float = 0.9):
    """Test-double transcriber for synthetic speech: which words are (nearly) whole?"""

    def run(voice: np.ndarray, sr: int) -> list[str]:
        win = 4096  # ~12 Hz bins: word tones are 61 Hz apart
        hop = 480
        if len(voice) < win:
            return []
        frames = 1 + (len(voice) - win) // hop
        idx = np.arange(win)[None, :] + hop * np.arange(frames)[:, None]
        spec = np.abs(np.fft.rfft(voice[idx] * np.hanning(win), axis=1))
        freqs = np.fft.rfftfreq(win, 1 / sr)
        found: list[tuple[int, str]] = []
        for tr in truths:
            b = int(np.argmin(np.abs(freqs - tr.freq)))
            band = spec[:, max(0, b - 1) : b + 2].max(axis=1)
            # Absolute level: a real word burst is ~250 here; leakage from neighbours is < 10.
            on = band > 40.0
            present_s = on.sum() * hop / sr
            if present_s >= min_presence * (tr.end - tr.start) - 0.03:
                found.append((int(np.argmax(on)), tr.text))
        return [t for _, t in sorted(found)]

    return run


def sentence(words: str, gap: float = 0.05, dur: float = 0.28, end_gap: float = 0.35):
    toks = words.split()
    return [(w, dur, end_gap if k == len(toks) - 1 else gap) for k, w in enumerate(toks)]
