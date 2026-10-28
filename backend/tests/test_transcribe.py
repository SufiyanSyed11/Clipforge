"""P1-3: transcription config, chunking and chunk merging (no model needed)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from clipforge.core import transcribe
from clipforge.models import Segment, Word
from clipforge.settings import Settings


def _settings(**kw) -> Settings:
    return Settings(**kw)


ENV_GPU = {
    "defaults": {
        "whisper": {
            "model": "large-v3",
            "device": "cuda",
            "compute_type": "float16",
            "reason": "GPU",
        }
    }
}


def test_auto_config_follows_env_report():
    cfg = transcribe.resolve_config(_settings(), ENV_GPU)
    assert (cfg.model, cfg.device, cfg.compute_type) == ("large-v3", "cuda", "float16")
    assert cfg.chunk_seconds == 0  # GPU: single pass


def test_no_env_report_is_safe_cpu_default_with_chunking():
    cfg = transcribe.resolve_config(_settings(), None)
    assert cfg.device == "cpu" and cfg.compute_type == "int8"
    assert cfg.chunk_seconds == 600
    assert "clipforge-check" in cfg.reason


def test_env_overrides_win():
    cfg = transcribe.resolve_config(
        _settings(whisper_model="medium", whisper_device="cpu", whisper_compute_type="int8"),
        ENV_GPU,
    )
    assert (cfg.model, cfg.device) == ("medium", "cpu")
    assert cfg.reason == "set in .env"


def test_plan_chunks_covers_audio_with_overlap():
    chunks = transcribe.plan_chunks(1500, 600, 5)
    assert chunks[0] == (0, 600)
    assert chunks[1][0] == 595
    assert chunks[-1][1] == 1500
    for (_a0, a1), (b0, _b1) in zip(chunks, chunks[1:], strict=False):
        assert b0 == a1 - 5
    assert transcribe.plan_chunks(100, 600, 5) == [(0.0, 100)]
    with pytest.raises(ValueError):
        transcribe.plan_chunks(1000, 10, 10)


def _seg(words: list[tuple[str, float, float]]) -> Segment:
    ws = [Word(text=t, start=s, end=e) for t, s, e in words]
    return Segment(start=ws[0].start, end=ws[-1].end, text=" ".join(w.text for w in ws), words=ws)


def test_merge_keeps_each_overlap_word_exactly_once():
    # Chunks [0, 10) and [8, 18): overlap 8-10, split at 9.
    a = _seg([("one", 7.0, 7.5), ("two", 8.2, 8.6), ("three", 9.1, 9.5)])
    b = _seg([("two", 8.25, 8.6), ("three", 9.1, 9.55), ("four", 10.2, 10.6)])
    merged = transcribe.merge_chunks([(0, 10, [a]), (8, 18, [b])])
    texts = [w.text for s in merged for w in s.words]
    assert texts == ["one", "two", "three", "four"]


class _FakeWhisper:
    """Pretends to be faster-whisper: one word per second of the chunk it is given."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        assert kw["word_timestamps"] is True and kw["vad_filter"] is True
        secs = len(audio) / transcribe.SAMPLE_RATE
        words = [
            SimpleNamespace(word=f" w{i}", start=i + 0.1, end=i + 0.5, probability=0.9)
            for i in range(int(secs))
        ]
        return [SimpleNamespace(words=words)], SimpleNamespace(language="en")


def test_chunked_transcription_has_no_gaps_or_duplicates():
    audio = np.zeros(transcribe.SAMPLE_RATE * 25, dtype=np.float32)
    cfg = transcribe.WhisperConfig("small", "cpu", "int8", chunk_seconds=10, overlap_seconds=2)
    model = _FakeWhisper()
    t = transcribe.transcribe(audio, cfg, lambda m, p: None, model=model)
    assert model.calls == 3
    starts = [w.start for w in t.words]
    assert starts == sorted(starts)
    # One word per second of audio, each exactly once.
    assert [int(s) for s in starts] == list(range(25))
    assert transcribe.transcript_qa(t).passed
    assert t.language == "en"


def test_fake_transcript_is_marked_and_fails_real_check():
    t = transcribe.fake_transcript(60)
    assert t.fake
    qa = transcribe.transcript_qa(t)
    assert not qa.passed
    assert qa.failures[0].name == "real transcription"
