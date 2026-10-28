"""Speaker diarization and host suggestion.

Backends:
- `basic` (built in, no PyTorch): voice features (MFCC mean/std) per speech window, clustered.
  Good for two clearly different voices; weak on crosstalk. The host is always confirmed by
  the owner, so a wrong guess is caught before clip picking.
- `pyannote` (optional): pyannote/speaker-diarization-3.1, needs PyTorch + a Hugging Face token.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from clipforge.models import HostSuggestion, Segment, SpeakerStats, Transcript, Word
from clipforge.qa import QAReport

SAMPLE_RATE = 16000
WINDOW_S = 1.5  # analysis window inside speech
# Silhouette below this: treat as one voice. Kept low on purpose: merging two real speakers
# breaks host logic, while a wrongly split monologue is obvious at host confirmation.
MIN_SPLIT_SILHOUETTE = 0.08
# Silhouette below this: speakers are only weakly separated; QA warns and asks for a check.
# Calibrated on synthetic voices only (distinct voices ~0.9, one voice ~0.2); verify on the
# golden episodes.
WEAK_SEPARATION = 0.35
Logger = Callable[[str, float | None], None]


# ---- features -------------------------------------------------------------------------------


def _mel_filterbank(n_mels: int, n_fft: int, sr: int, fmin: float = 60, fmax: float = 7600):
    def hz_to_mel(f):
        return 2595 * np.log10(1 + f / 700)

    def mel_to_hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    bins = np.floor((n_fft + 1) * mel_to_hz(mels) / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        for k in range(left, center):
            fb[m - 1, k] = (k - left) / max(center - left, 1)
        for k in range(center, right):
            fb[m - 1, k] = (right - k) / max(right - center, 1)
    return fb


_FB = _mel_filterbank(40, 512, SAMPLE_RATE)


def mfcc(audio: np.ndarray, n_mfcc: int = 20) -> np.ndarray:
    """MFCC frames (25 ms window, 10 ms hop). Returns (frames, n_mfcc)."""
    from scipy.fft import dct

    win, hop, n_fft = 400, 160, 512
    if len(audio) < win:
        return np.zeros((0, n_mfcc))
    audio = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])  # pre-emphasis
    n = 1 + (len(audio) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = audio[idx] * np.hamming(win)
    power = np.abs(np.fft.rfft(frames, n_fft)) ** 2 / n_fft
    mel = np.log(power @ _FB.T + 1e-10)
    return dct(mel, type=2, axis=1, norm="ortho")[:, :n_mfcc]


@dataclass
class _Window:
    start: float
    end: float
    seg_index: int


def _speech_windows(segments: list[Segment]) -> list[_Window]:
    out = []
    for i, seg in enumerate(segments):
        t = seg.start
        while t < seg.end - 0.3:
            end = min(t + WINDOW_S, seg.end)
            out.append(_Window(t, end, i))
            t = end
    return out


def _window_features(audio: np.ndarray, windows: list[_Window]) -> np.ndarray:
    feats = []
    for w in windows:
        piece = audio[int(w.start * SAMPLE_RATE) : int(w.end * SAMPLE_RATE)]
        m = mfcc(piece)
        if len(m) == 0:
            feats.append(np.zeros(40))
            continue
        # Drop c0 (loudness) so level differences don't decide the speaker.
        feats.append(np.concatenate([m[:, 1:].mean(0), m[:, 1:].std(0), [0.0, 0.0]]))
    x = np.array(feats)
    return (x - x.mean(0)) / (x.std(0) + 1e-8)


def _cluster(x: np.ndarray, max_speakers: int) -> tuple[np.ndarray, float | None]:
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score

    if len(x) < 4:
        return np.zeros(len(x), dtype=int), None
    best_labels, best_score = np.zeros(len(x), dtype=int), -1.0
    for k in range(2, min(max_speakers, len(x) - 1) + 1):
        labels = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(x)
        score = silhouette_score(x, labels)
        if score > best_score:
            best_labels, best_score = labels, score
    if best_score < MIN_SPLIT_SILHOUETTE:
        return np.zeros(len(x), dtype=int), best_score
    return best_labels, best_score


def _smooth(labels: np.ndarray, size: int = 3) -> np.ndarray:
    """Majority vote over neighbouring windows to remove one-window flips."""
    if len(labels) < size:
        return labels
    out = labels.copy()
    half = size // 2
    for i in range(len(labels)):
        lo, hi = max(0, i - half), min(len(labels), i + half + 1)
        vals, counts = np.unique(labels[lo:hi], return_counts=True)
        out[i] = vals[counts.argmax()]
    return out


@dataclass
class DiarizeResult:
    turns: list[tuple[float, float, str]]  # (start, end, label) in episode time
    # Silhouette of the speaker clustering (basic backend); None when not measured.
    separation: float | None = None


def diarize_basic(
    audio: np.ndarray, transcript: Transcript, max_speakers: int = 3
) -> DiarizeResult:
    windows = _speech_windows(transcript.segments)
    if not windows:
        return DiarizeResult([])
    raw, separation = _cluster(_window_features(audio, windows), max_speakers)
    labels = _smooth(raw)
    # Name speakers S1, S2, ... in order of first appearance.
    names: dict[int, str] = {}
    for lab in labels:
        names.setdefault(int(lab), f"S{len(names) + 1}")
    turns = [(w.start, w.end, names[int(lab)]) for w, lab in zip(windows, labels, strict=True)]
    return DiarizeResult(turns, None if separation is None else round(float(separation), 3))


def diarize_pyannote(wav_path: str, hf_token: str) -> DiarizeResult:
    from pyannote.audio import Pipeline  # optional dependency

    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)
    result = pipeline(wav_path)
    names: dict[str, str] = {}
    turns = []
    for turn, _, label in result.itertracks(yield_label=True):
        names.setdefault(label, f"S{len(names) + 1}")
        turns.append((float(turn.start), float(turn.end), names[label]))
    return DiarizeResult(turns)


# ---- assigning words ------------------------------------------------------------------------


def assign_speakers(transcript: Transcript, turns: list[tuple[float, float, str]]) -> Transcript:
    """Label every word by the turn covering its centre (nearest turn if none covers it),
    then split segments where the speaker changes."""
    if not turns:
        turns = [(0.0, transcript.duration, "S1")]
    turns = sorted(turns)
    starts = np.array([t[0] for t in turns])

    def speaker_at(t: float) -> str:
        i = int(np.searchsorted(starts, t, side="right")) - 1
        candidates = [turns[j] for j in (i, i + 1) if 0 <= j < len(turns)]
        for s, e, lab in candidates:
            if s <= t <= e:
                return lab
        return min(candidates, key=lambda c: min(abs(c[0] - t), abs(c[1] - t)))[2]

    new_segments: list[Segment] = []
    for seg in transcript.segments:
        current: list[Word] = []
        for w in seg.words:
            w = w.model_copy(update={"speaker": speaker_at((w.start + w.end) / 2)})
            if current and current[-1].speaker != w.speaker:
                new_segments.append(_seg(current))
                current = []
            current.append(w)
        if current:
            new_segments.append(_seg(current))
    return transcript.model_copy(update={"segments": new_segments})


def _seg(words: list[Word]) -> Segment:
    return Segment(
        start=words[0].start,
        end=words[-1].end,
        text=" ".join(w.text for w in words),
        speaker=words[0].speaker,
        words=words,
    )


# ---- host suggestion ------------------------------------------------------------------------

_HOST_PHRASES = re.compile(
    r"\b(welcome (back )?to|my guest|our guest|joining me|on the show|thanks for (coming|having)"
    r"|today we have|let'?s get into|before we (start|begin)|tell (me|us) about)\b",
    re.IGNORECASE,
)
_GUEST_PHRASES = re.compile(r"\bthanks for having me\b", re.IGNORECASE)


def speaker_stats(t: Transcript) -> list[SpeakerStats]:
    total = sum(w.end - w.start for w in t.words) or 1.0
    by: dict[str, dict] = {}
    prev = None
    for seg in t.segments:
        sp = seg.speaker or "S1"
        d = by.setdefault(
            sp, {"talk": 0.0, "turns": 0, "q": 0, "first": seg.start, "sample": seg.text}
        )
        d["talk"] += sum(w.end - w.start for w in seg.words)
        d["q"] += seg.text.count("?")
        if sp != prev:
            d["turns"] += 1
        if len(d["sample"]) < 40 and len(seg.text) > len(d["sample"]):
            d["sample"] = seg.text
        prev = sp
    return [
        SpeakerStats(
            speaker=sp,
            talk_seconds=round(d["talk"], 1),
            talk_share=round(d["talk"] / total, 3),
            turns=d["turns"],
            questions=d["q"],
            first_spoke_at=round(d["first"], 2),
            sample=d["sample"][:200],
        )
        for sp, d in sorted(by.items())
    ]


def suggest_host(t: Transcript, stats: list[SpeakerStats]) -> HostSuggestion | None:
    if not stats:
        return None
    if len(stats) == 1:
        return HostSuggestion(
            speaker=stats[0].speaker,
            confidence="low",
            reasons=["only one voice was detected; check whether this is a solo episode"],
        )
    scores: dict[str, float] = {s.speaker: 0.0 for s in stats}
    reasons: dict[str, list[str]] = {s.speaker: [] for s in stats}
    first = min(stats, key=lambda s: s.first_spoke_at)
    scores[first.speaker] += 1.0
    reasons[first.speaker].append("speaks first")
    most_q = max(stats, key=lambda s: s.questions)
    if most_q.questions > 0:
        scores[most_q.speaker] += 1.5
        reasons[most_q.speaker].append(f"asks the most questions ({most_q.questions})")
    for seg in t.segments[:40]:
        sp = seg.speaker or "S1"
        if _HOST_PHRASES.search(seg.text) and not _GUEST_PHRASES.search(seg.text):
            if "uses host phrases (welcome, my guest, ...)" not in reasons[sp]:
                scores[sp] += 2.0
                reasons[sp].append("uses host phrases (welcome, my guest, ...)")
        if _GUEST_PHRASES.search(seg.text):
            scores[sp] -= 1.0
    least_talk = min(stats, key=lambda s: s.talk_share)
    if least_talk.talk_share < 0.45:
        scores[least_talk.speaker] += 0.5
        reasons[least_talk.speaker].append(
            f"talks less ({least_talk.talk_share:.0%}), as hosts usually do"
        )
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    margin = best_score - ranked[1][1]
    confidence = "high" if margin >= 2 else "medium" if margin >= 1 else "low"
    return HostSuggestion(
        speaker=best, confidence=confidence, reasons=reasons[best] or ["no strong signal"]
    )


def diarize_qa(
    t: Transcript, stats: list[SpeakerStats], backend: str, separation: float | None = None
) -> QAReport:
    qa = QAReport(step="diarize")
    words = t.words
    qa.check(
        "every word has a speaker",
        all(w.speaker for w in words),
        f"{len(words)} words, backend {backend}",
    )
    qa.check(
        "more than one speaker found",
        len(stats) > 1,
        ", ".join(f"{s.speaker} {s.talk_share:.0%}" for s in stats),
        severity="warn",
    )
    if separation is not None and len(stats) > 1:
        qa.check(
            "speakers clearly separated",
            separation >= WEAK_SEPARATION,
            f"separation score {separation:.2f} (weak below {WEAK_SEPARATION}); check the "
            "speaker samples before confirming the host",
            severity="warn",
        )
    if backend == "basic":
        qa.check(
            "diarization quality",
            True,
            "basic backend: reliable for distinct voices, weak on crosstalk; confirm the host",
            severity="warn",
        )
    return qa


def fake_turns(transcript: Transcript) -> DiarizeResult:
    """Test stub: alternate speakers by segment (lines with '?' are the host)."""
    turns = []
    for seg in transcript.segments:
        sp = "S1" if "?" in seg.text or "Welcome" in seg.text else "S2"
        turns.append((seg.start, seg.end, sp))
    return DiarizeResult(turns)
