"""P1-4: diarization and host suggestion."""

from __future__ import annotations

import numpy as np

from clipforge.core import diarize
from clipforge.models import Segment, Transcript, Word

SR = diarize.SAMPLE_RATE


def _voice(seconds: float, f0: float, formants: list[float], seed: int) -> np.ndarray:
    """Crude synthetic voice: harmonic source with vibrato, shaped by formant peaks."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    pitch = f0 * (1 + 0.03 * np.sin(2 * np.pi * 5 * t))
    phase = 2 * np.pi * np.cumsum(pitch) / SR
    sig = np.zeros_like(t)
    for h in range(1, 30):
        freq = f0 * h
        if freq > 7000:
            break
        gain = sum(np.exp(-((freq - f) ** 2) / (2 * 120**2)) for f in formants) + 0.02
        sig += gain * np.sin(h * phase)
    sig += 0.01 * rng.standard_normal(len(t))
    # Syllable-like amplitude envelope.
    sig *= 0.6 + 0.4 * np.abs(np.sin(2 * np.pi * 3 * t))
    return (0.3 * sig / np.abs(sig).max()).astype(np.float32)


def _conversation(turns: list[tuple[str, float]]):
    voices = {
        "A": dict(f0=110, formants=[500, 1500, 2500]),
        "B": dict(f0=210, formants=[800, 2000, 3200]),
    }
    audio, segments, truth = [], [], []
    t = 0.0
    for i, (who, dur) in enumerate(turns):
        audio.append(_voice(dur, seed=i, **voices[who]))
        words = [
            Word(text=f"w{j}", start=round(t + j * 0.5, 3), end=round(t + j * 0.5 + 0.4, 3))
            for j in range(int(dur / 0.5))
        ]
        segments.append(
            Segment(start=t, end=t + dur, text=" ".join(w.text for w in words), words=words)
        )
        truth.append((t, t + dur, who))
        t += dur
        gap = np.zeros(int(0.3 * SR), dtype=np.float32)
        audio.append(gap)
        t += 0.3
    tr = Transcript(duration=t, model="test", device="cpu", segments=segments)
    return np.concatenate(audio), tr, truth


def test_basic_backend_separates_two_distinct_voices():
    pattern = [("A", 4), ("B", 5), ("A", 3), ("B", 6), ("A", 5), ("B", 4), ("A", 4), ("B", 3)]
    audio, tr, truth = _conversation(pattern)
    result = diarize.diarize_basic(audio, tr)
    assert result.separation >= diarize.WEAK_SEPARATION
    labelled = diarize.assign_speakers(tr, result.turns)
    # Compare per word, allowing for label permutation.
    pairs = []
    for w in labelled.words:
        mid = (w.start + w.end) / 2
        who = next(lab for s, e, lab in truth if s <= mid <= e)
        pairs.append((who, w.speaker))
    mapping = {"A": "S1", "B": "S2"}
    acc = np.mean([mapping[a] == b for a, b in pairs])
    acc = max(acc, 1 - acc)
    assert acc >= 0.9, f"accuracy {acc:.2f}"
    assert all(w.speaker for w in labelled.words)


def test_single_voice_is_flagged_as_weakly_separated():
    audio, tr, _ = _conversation([("A", 5), ("A", 6), ("A", 4)])
    result = diarize.diarize_basic(audio, tr)
    labelled = diarize.assign_speakers(tr, result.turns)
    stats = diarize.speaker_stats(labelled)
    qa = diarize.diarize_qa(labelled, stats, "basic", result.separation)
    if len(stats) > 1:
        # A monologue split into two must never look trustworthy.
        weak = next(c for c in qa.checks if c.name == "speakers clearly separated")
        assert not weak.passed


def _tr(lines: list[tuple[str, str]]) -> Transcript:
    segs = []
    t = 0.0
    for sp, text in lines:
        words = []
        for tok in text.split():
            words.append(Word(text=tok, start=t, end=t + 0.3, speaker=sp))
            t += 0.35
        segs.append(
            Segment(start=words[0].start, end=words[-1].end, text=text, speaker=sp, words=words)
        )
        t += 0.5
    return Transcript(duration=t + 1, model="test", device="cpu", segments=segs)


def test_host_suggestion_uses_host_signals():
    tr = _tr(
        [
            ("S1", "Welcome back to the show, my guest today is Dana."),
            ("S2", "Thanks for having me, it's great to be here with you all today."),
            ("S1", "So how did it start?"),
            ("S2", "It started with a bet I made in college with my roommate, a crazy bet."),
            ("S1", "Wait, what was the bet?"),
            ("S2", "That I could sell a thousand shirts in one week, and I actually did it."),
        ]
    )
    stats = diarize.speaker_stats(tr)
    s = diarize.suggest_host(tr, stats)
    assert s.speaker == "S1"
    assert s.confidence == "high"
    assert any("question" in r for r in s.reasons)


def test_host_suggestion_single_speaker_is_low_confidence():
    tr = _tr([("S1", "Today I want to talk about money."), ("S1", "Here is my story.")])
    s = diarize.suggest_host(tr, diarize.speaker_stats(tr))
    assert s.confidence == "low"


def test_assign_speakers_splits_segments_at_speaker_change():
    tr = _tr([("S1", "one two three four")])
    tr = tr.model_copy(
        update={"segments": [s.model_copy(update={"speaker": None}) for s in tr.segments]}
    )
    turns = [(0.0, 0.6, "S1"), (0.6, 5.0, "S2")]
    out = diarize.assign_speakers(tr, turns)
    assert [s.speaker for s in out.segments] == ["S1", "S2"]
    qa = diarize.diarize_qa(out, diarize.speaker_stats(out), "basic")
    assert qa.passed
