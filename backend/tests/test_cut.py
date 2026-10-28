"""Phase 2: cut-point engine + verification gate on synthetic speech.

Synthetic words are tone bursts with known true extents. "Zero clipped words" is checked two
ways: every cut lies outside every word's true extent, and the test-double transcriber hears
every intended word whole in the assembled voice track.
"""

from __future__ import annotations

import numpy as np
import pytest

from clipforge.core import verify
from clipforge.core.cut import CutConfig, CutEngine, CutError, censor
from clipforge.core.voice import assemble_voice
from tests.speech import decode_words, make_speech, sentence

FPS = 60


def _story(n_sentences: int = 8, gap: float = 0.05, end_gap: float = 0.35):
    text = [
        "Nobody knew I almost quit that night.",
        "I had two million dollars on the table.",
        "My coach said one sentence to me.",
        "You can buy a house but not this moment.",
        "So I stayed and we played the final.",
        "We won the game by a single point.",
        "My mother did not talk to me for weeks.",
        "Then she saw the trophy and cried.",
        "That was the best day of my life.",
        "I still think about it every morning.",
    ][:n_sentences]
    words = []
    for s in text:
        words += sentence(s, gap=gap, end_gap=end_gap)
    return words


def _sent(truths, n: int) -> tuple[int, int]:
    """Indices of the first and last word of sentence n (0-based)."""
    ends = [i for i, t in enumerate(truths) if t.text.endswith((".", "?", "!"))]
    first = 0 if n == 0 else ends[n - 1] + 1
    return first, ends[n]


def _inside_any_word(t: float, truths, margin: float = 0.0) -> bool:
    return any(tr.start - margin < t < tr.end + margin for tr in truths)


def _engine(audio, tr, **cfg):
    return CutEngine(audio, tr, CutConfig(fps=FPS, min_clip=5, **cfg))


def _assert_clean(res, audio, truths, expect_words):
    spec = res.spec
    for c in spec.cuts:
        assert not _inside_any_word(c.src_time, truths, margin=0.03), f"cut inside a word at {c}"
        assert abs(c.src_time * FPS - round(c.src_time * FPS)) < 1e-6  # frame grid
    voice = assemble_voice(audio, spec)
    assert len(voice) == round(spec.duration * 48000)
    qa = verify.verify_voice(voice, spec, decode_words(truths), transcriber_is_real=False)
    heard = decode_words(truths)(voice, 48000)
    assert heard == expect_words, f"heard {heard}"
    failing = [c for c in qa.failures if c.name != "re-transcription by a real model"]
    assert not failing, failing
    return spec, voice, qa


def test_clean_audio_cuts_in_real_gaps_despite_whisper_errors():
    audio, tr, truths = make_speech(_story(), whisper_error=0.3, seed=1)
    words = tr.words
    # Ask for sentences 2-4 using whisper (drifted, jittered) times.
    a, _ = _sent(truths, 1)
    _, b = _sent(truths, 3)
    res = _engine(audio, tr).build([(words[a].start, words[b].end)], "src.mp4")
    assert not res.flags  # real gaps exist; nothing should need extending
    spec, _, _ = _assert_clean(res, audio, truths, [t.text for t in truths[a : b + 1]])
    # Lead-in and tail stay short: hook lands fast, last word decays naturally.
    first, last = truths[a], truths[b]
    assert 0.06 <= first.start - spec.pieces[0].src_start <= 0.21
    assert 0.06 <= spec.pieces[-1].src_end - last.end <= 0.41


def test_noisy_floor_judged_by_dips():
    # Floor around int16 RMS 300 (-41 dBFS): an absolute threshold would see no silence.
    audio, tr, truths = make_speech(_story(), noise_rms=300 / 32768, whisper_error=0.3, seed=2)
    words = tr.words
    a, _ = _sent(truths, 1)
    _, b = _sent(truths, 2)
    res = _engine(audio, tr).build([(words[a].start, words[b].end)], "src.mp4")
    _assert_clean(res, audio, truths, [t.text for t in truths[a : b + 1]])
    assert all(c.threshold_db > -45 for c in res.spec.cuts)  # threshold follows the floor


def test_no_real_gap_extends_to_next_natural_pause():
    # Sentence 3 runs straight into sentence 4 with no pause (crosstalk-like).
    base = make_speech(_story(), seed=3)[2]
    s3a, s3b = _sent(base, 2)
    s4a, s4b = _sent(base, 4)
    words = _story()
    words[s3b] = (words[s3b][0], words[s3b][1], 0.04)  # sentence 3 runs straight into 4
    audio, tr, truths = make_speech(words, whisper_error=0.1, seed=3)
    w = tr.words
    # Ask to start at sentence 4 (no gap before it): must move back to before sentence 3.
    res = _engine(audio, tr).build([(w[s3b + 1].start, w[s4b].end)], "src.mp4")
    assert res.spec.pieces[0].src_start < truths[s3a].start
    assert any("natural pause" in f for f in res.flags)
    _assert_clean(res, audio, truths, [t.text for t in truths[s3a : s4b + 1]])


def test_no_pause_anywhere_fails_loudly_with_timestamp():
    words = [(f"w{i}", 0.25, 0.03) for i in range(60)]
    audio, tr, _ = make_speech(words, lead=0.05, tail=0.05, whisper_error=0.05, seed=4)
    w = tr.words
    with pytest.raises(CutError) as e:
        _engine(audio, tr).build([(w[20].start, w[40].end)], "src.mp4")
    assert e.value.at_seconds is not None
    assert "No silence" in str(e.value)


def test_long_pauses_are_shortened_inside_strict_silence():
    base = make_speech(_story(), seed=5)[2]
    a, k = _sent(base, 1)
    _, b = _sent(base, 3)
    words = _story()
    words[k] = (words[k][0], words[k][1], 1.5)  # a 1.5 s pause after sentence 2
    audio, tr, truths = make_speech(words, whisper_error=0.2, seed=5)
    w = tr.words
    res = _engine(audio, tr).build([(w[a].start, w[b].end)], "src.mp4")
    spec, _voice, qa = _assert_clean(res, audio, truths, [t.text for t in truths[a : b + 1]])
    assert len(spec.pieces) >= 2
    assert any(j.reason == "pause" for j in spec.joins)
    removed = sum(
        b.src_start - a.src_end for a, b in zip(spec.pieces, spec.pieces[1:], strict=False)
    )
    assert removed > 0.9
    assert any(c.name.startswith("join at") for c in qa.checks)


def test_stitched_windows_join_in_silence():
    audio, tr, truths = make_speech(_story(10), whisper_error=0.25, seed=6)
    w = tr.words
    a0, _ = _sent(truths, 0)
    _, b0 = _sent(truths, 1)
    a1, _ = _sent(truths, 7)
    _, b1 = _sent(truths, 8)
    res = _engine(audio, tr).build([(w[a0].start, w[b0].end), (w[a1].start, w[b1].end)], "src.mp4")
    expect = [t.text for t in truths[a0 : b0 + 1]] + [t.text for t in truths[a1 : b1 + 1]]
    spec, _, _ = _assert_clean(res, audio, truths, expect)
    assert any(j.reason == "seam" for j in spec.joins)


def test_profanity_in_middle_sentence_is_skipped_with_a_different_seam():
    base = make_speech(_story(), seed=7)[2]
    a, _ = _sent(base, 1)
    m0, m1 = _sent(base, 2)
    _, b = _sent(base, 4)
    words = _story()
    words[m0 + 2] = ("fucking", words[m0 + 2][1], words[m0 + 2][2])  # inside sentence 3
    audio, tr, truths = make_speech(words, whisper_error=0.2, seed=7)
    w = tr.words
    res = _engine(audio, tr).build([(w[a].start, w[b].end)], "src.mp4")
    spec = res.spec
    assert not spec.bleeps
    assert spec.removed and "fucking" in spec.removed[0]["text"]
    expect = [t.text for t in truths[a:m0]] + [t.text for t in truths[m1 + 1 : b + 1]]
    _assert_clean(res, audio, truths, expect)
    assert "fucking" not in [c.spoken for c in spec.captions]


def test_profanity_in_the_payoff_is_bleeped_not_cut():
    base = make_speech(_story(), seed=8)[2]
    a, _ = _sent(base, 1)
    _, e = _sent(base, 3)
    k = e - 2  # inside the last sentence of the clip = the payoff
    words = _story()
    words[k] = ("shit", words[k][1], words[k][2])
    audio, tr, truths = make_speech(words, whisper_error=0.2, seed=8)
    w = tr.words
    res = _engine(audio, tr).build([(w[a].start, w[e].end)], "src.mp4")
    spec = res.spec
    assert len(spec.bleeps) == 1 and spec.bleeps[0].word == "shit"
    b = spec.bleeps[0]
    tw = truths[k]
    # The bleep covers the whole word and doesn't touch its neighbours.
    assert b.src_start <= tw.start + 0.02 and b.src_end >= tw.end - 0.02
    assert b.src_start >= truths[k - 1].end - 0.02 and b.src_end <= truths[k + 1].start + 0.02
    cap = next(c for c in spec.captions if c.spoken == "shit")
    assert cap.censored and cap.text == "s**t"
    voice = assemble_voice(audio, spec)
    heard = decode_words(truths)(voice, 48000)
    assert "shit" not in heard  # muted under the tone
    assert [t.text for t in truths[a : e + 1] if t.text != "shit"] == heard  # neighbours intact
    qa = verify.verify_voice(voice, spec, decode_words(truths), transcriber_is_real=False)
    assert not [c for c in qa.failures if c.name != "re-transcription by a real model"]


def test_profanity_modes_bleep_and_allow():
    base = make_speech(_story(), seed=9)[2]
    a, _ = _sent(base, 1)
    m0, _ = _sent(base, 2)
    _, b = _sent(base, 4)
    words = _story()
    words[m0 + 2] = ("fucking", words[m0 + 2][1], words[m0 + 2][2])
    audio, tr, _ = make_speech(words, whisper_error=0.2, seed=9)
    w = tr.words
    seg = [(w[a].start, w[b].end)]
    assert len(_engine(audio, tr, profanity_mode="bleep").build(seg, "s").spec.bleeps) == 1
    res = _engine(audio, tr, profanity_mode="allow").build(seg, "s")
    assert not res.spec.bleeps and any("left in" in f for f in res.flags)


def test_over_30s_after_cutting_is_refused():
    audio, tr, truths = make_speech(_story(10) * 2, whisper_error=0.1, seed=10)
    w = tr.words
    _, b = _sent(truths, 13)
    with pytest.raises(CutError, match="30 s hard max"):
        _engine(audio, tr).build([(w[0].start, w[b].end)], "s")


def test_trim_handles_snap_to_silence():
    audio, tr, truths = make_speech(_story(), whisper_error=0.2, seed=11)
    eng = _engine(audio, tr)
    # Drag a handle into the middle of a word: it must land in a real gap.
    mid_word = (truths[10].start + truths[10].end) / 2
    cut, _gap = eng.snap_to_silence(mid_word, "start")
    assert not _inside_any_word(cut.src_time, truths, margin=0.03)
    a, _ = _sent(truths, 1)
    s3, _ = _sent(truths, 2)
    _, b = _sent(truths, 3)
    w = tr.words
    res = eng.build([(w[a].start, w[b].end)], "s", trims={0: (truths[s3].start - 0.1, None)})
    assert truths[s3 - 1].end < res.spec.pieces[0].src_start < truths[s3].start


def test_verification_catches_a_clipped_word():
    audio, tr, truths = make_speech(_story(), whisper_error=0.2, seed=12)
    w = tr.words
    a, _ = _sent(truths, 1)
    _, b = _sent(truths, 3)
    res = _engine(audio, tr).build([(w[a].start, w[b].end)], "s")
    spec = res.spec
    # Sabotage: move the end cut into the middle of the last word.
    bad = spec.model_copy(deep=True)
    last = truths[b]
    bad.pieces[-1].src_end = round(((last.start + last.end) / 2) * FPS) / FPS
    voice = assemble_voice(audio, bad)
    qa = verify.verify_voice(voice, bad, decode_words(truths), transcriber_is_real=False)
    names = {c.name for c in qa.failures}
    assert "last 0.45 s: last word decays into silence" in names
    assert "no word missing at a join or edge (zero clipped words)" in names
    clipped = next(c for c in qa.failures if c.name.startswith("no word missing"))
    assert clipped.at_seconds is not None


def test_verification_catches_a_join_inside_speech():
    audio, tr, truths = make_speech(_story(), whisper_error=0.2, seed=13)
    w = tr.words
    s1, _ = _sent(truths, 1)
    _, s3 = _sent(truths, 3)
    spec = _engine(audio, tr).build([(w[s1].start, w[s3].end)], "s").spec.model_copy(deep=True)
    from clipforge.editspec import Join, Piece

    a, b = spec.pieces[0].src_start, spec.pieces[0].src_end
    mid = round(((truths[s1 + 3].start + truths[s1 + 3].end) / 2) * FPS) / FPS
    spec.pieces = [Piece(src_start=a, src_end=mid), Piece(src_start=mid, src_end=b)]
    spec.joins = [Join(out_time=mid - a, src_before=mid, src_after=mid, reason="sabotage")]
    voice = assemble_voice(audio, spec)
    qa = verify.verify_voice(voice, spec, decode_words(truths), transcriber_is_real=False)
    join_check = next(c for c in qa.checks if c.name.startswith("join at"))
    assert not join_check.passed and join_check.at_seconds == pytest.approx(mid - a)


def test_no_transcriber_means_not_verified():
    audio, tr, truths = make_speech(_story(), seed=14)
    w = tr.words
    a, _ = _sent(truths, 1)
    _, b = _sent(truths, 3)
    spec = _engine(audio, tr).build([(w[a].start, w[b].end)], "s").spec
    qa = verify.verify_voice(assemble_voice(audio, spec), spec, None)
    assert not qa.passed
    assert "can't be ruled out" in qa.failures[-1].detail


def test_censor():
    assert censor("fuck") == "f**k"
    assert censor("Shit,") == "S**t,"
    assert censor("damn!") == "d**n!"


def test_voice_assembly_is_sample_exact_and_faded():
    audio, tr, truths = make_speech(_story(), seed=15)
    w = tr.words
    a, _ = _sent(truths, 1)
    _, b = _sent(truths, 3)
    spec = _engine(audio, tr).build([(w[a].start, w[b].end)], "s").spec
    voice = assemble_voice(audio, spec)
    assert len(voice) == spec.frame_count * 800  # 48000 / 60
    assert abs(voice[0]) < 1e-4 and abs(voice[-1]) < 1e-4
    assert np.isfinite(voice).all()


@pytest.mark.parametrize("noise", [0.0005, 100 / 32768, 400 / 32768])
@pytest.mark.parametrize("err", [0.1, 0.25, 0.4])
def test_stress_zero_clipped_words(noise, err):
    """Random clips over noise floors from studio-clean to int16 RMS 400 and whisper errors
    up to 0.4 s: no cut inside a word, every intended word heard, every gate check passes."""
    import random

    for seed in range(6):
        audio, tr, truths = make_speech(_story(10), noise_rms=noise, whisper_error=err, seed=seed)
        w = tr.words
        rnd = random.Random(seed)
        s0 = rnd.randint(0, 6)
        s1 = min(9, s0 + rnd.randint(1, 3))
        a, _ = _sent(truths, s0)
        _, b = _sent(truths, s1)
        res = _engine(audio, tr).build([(w[a].start, w[b].end)], "s")
        assert not [c for c in res.spec.cuts if _inside_any_word(c.src_time, truths)]
        voice = assemble_voice(audio, res.spec)
        heard = decode_words(truths)(voice, 48000)
        assert [t.text for t in truths[a : b + 1]] == heard
        qa = verify.verify_voice(voice, res.spec, decode_words(truths), transcriber_is_real=False)
        assert not [c for c in qa.failures if c.name != "re-transcription by a real model"]


def test_gate_fails_on_audible_profanity_unless_allowed():
    audio, tr, truths = make_speech(_story(), seed=15)
    w = tr.words
    a, _ = _sent(truths, 1)
    _, b = _sent(truths, 3)
    spec = _engine(audio, tr).build([(w[a].start, w[b].end)], "s").spec
    voice = assemble_voice(audio, spec)

    def leaky(v, sr):
        return decode_words(truths)(v, sr) + ["fucking"]

    qa = verify.verify_voice(voice, spec, leaky)
    leak = next(c for c in qa.checks if c.name == "no profanity audible in the cut")
    assert not leak.passed and "fucking" in leak.detail and leak.severity == "error"
    qa = verify.verify_voice(voice, spec, leaky, profanity_allowed=True)
    leak = next(c for c in qa.checks if c.name == "no profanity audible in the cut")
    assert leak.passed and "allowed" in leak.detail
    qa = verify.verify_voice(voice, spec, decode_words(truths))
    assert next(c for c in qa.checks if c.name == "no profanity audible in the cut").passed
