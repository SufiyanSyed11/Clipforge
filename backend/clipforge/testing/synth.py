"""Synthetic speech and fixture videos (TEST ONLY).

Word i is a tone burst at `freq(i)`; frequencies repeat every CYCLE words and stay below
8 kHz so they survive the 16 kHz transcription audio. Text for word i is SCRIPT[i % CYCLE],
so a decoder can recover both timing and text from the audio alone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

BASE_HZ = 400.0
STEP_HZ = 50.0
CYCLE = 120  # 400 + 50 * 119 = 6350 Hz < 8 kHz

_STORY = (
    "Welcome back to the show, today my guest is Marcus Hale. "
    "Thanks for having me, it is great to be here. "
    "So Marcus, what really happened the night before the final? "
    "Nobody knows this, but I almost walked away from the whole thing. "
    "I had a two million dollar offer and I said no in five minutes. "
    "Wait, you turned down two million dollars? "
    "My coach told me one sentence that changed everything. "
    "He said you can buy a house but you cannot buy this moment. "
    "And the next day we won the final by a single point. "
    "That is unbelievable, what did your family say about it? "
    "My mother did not talk to me for a week. "
    "Then she saw the trophy and she cried for an hour. "
    "What would you tell a young player who gets that same offer? "
    "Take the moment, the money comes back but the moment never does. "
    "Marcus Hale, thank you so much for coming on the show today. "
)
SCRIPT: list[str] = (_STORY.split() * 3)[:CYCLE]
# Host lines (questions) are spoken by S1, answers by S2: sentence index parity.
HOST_SENTENCES = {0, 2, 5, 9, 12, 14}


def freq(i: int) -> float:
    return BASE_HZ + STEP_HZ * (i % CYCLE)


def make_audio(seconds: float, sr: int = 48000, noise_rms: float = 0.0008, seed: int = 0):
    """Speech-like audio filling `seconds`; returns (audio, words[(text, start, end, speaker)])."""
    rng = np.random.default_rng(seed)
    audio = (rng.standard_normal(int(seconds * sr)) * noise_rms).astype(np.float32)
    words = []
    t, i, sentence = 0.6, 0, 0
    while True:
        text = SCRIPT[i % CYCLE]
        d = 0.22 + 0.02 * min(len(text), 8)
        if t + d > seconds - 0.5:
            break
        n = int(d * sr)
        tt = np.arange(n) / sr
        env = np.minimum(1.0, np.minimum(tt / 0.012, (d - tt) / 0.012))
        s = int(t * sr)
        audio[s : s + n] += (0.25 * env * np.sin(2 * np.pi * freq(i) * tt)).astype(np.float32)
        speaker = "S1" if sentence % 15 in HOST_SENTENCES else "S2"
        words.append((text, t, t + d, speaker))
        ends = text.endswith((".", "?", "!"))
        t += d + (0.45 if ends else 0.06)
        if ends:
            sentence += 1
        i += 1
    return audio, words


def write_fixture_video(path: Path, seconds: float = 75.0, seed: int = 0) -> Path:
    """A test-pattern video whose audio is synthetic speech (for FAKE_MODELS e2e runs)."""
    from clipforge.core.audio import write_wav

    path.parent.mkdir(parents=True, exist_ok=True)
    audio, _ = make_audio(seconds, seed=seed)
    wav = path.with_suffix(".speech.wav")
    write_wav(wav, audio)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
            "-i", str(wav),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    wav.unlink(missing_ok=True)
    return path


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Write a synthetic-speech fixture video (tests)")
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=float, default=75.0)
    args = ap.parse_args()
    write_fixture_video(args.out, args.seconds)


if __name__ == "__main__":
    main()
