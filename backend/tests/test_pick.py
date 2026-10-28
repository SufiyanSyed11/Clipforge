"""P1-6: clip picker validation and hard rules (recorded/fake model responses, no API)."""

from __future__ import annotations

import json
import re

import pytest

from clipforge.core import pick
from clipforge.llm.base import LLMError
from clipforge.llm.doubles import FakeLLM
from clipforge.models import Segment, Transcript, Word


def _transcript(seconds: int = 300) -> Transcript:
    """One word every 0.5 s; word 40 is a profanity."""
    segs = []
    for s0 in range(0, seconds, 10):
        words = []
        for k in range(20):
            i = s0 * 2 + k
            text = "fucking" if i == 40 else f"w{i}"
            words.append(Word(text=text, start=s0 + k * 0.5, end=s0 + k * 0.5 + 0.4))
        sp = "S1" if (s0 // 10) % 2 == 0 else "S2"
        segs.append(
            Segment(
                start=words[0].start,
                end=words[-1].end,
                text=" ".join(w.text for w in words),
                speaker=sp,
                words=[w.model_copy(update={"speaker": sp}) for w in words],
            )
        )
    return Transcript(duration=seconds, model="test", device="cpu", segments=segs, language="en")


def _clip(segments, score=8, risk=None):
    return {
        "segments": [{"start": a, "end": b, "text": "model text"} for a, b in segments],
        "hook": "hook",
        "context": "context",
        "payoff": "payoff",
        "virality_score": score,
        "reasons": ["r1", "r2", "r3"],
        "title_options": ["t1 🔥", '"t2" 😮', "t3 🎯"],
        "broll_beats": ["beat"],
        "risk_flags": risk or [],
        "improvements": "none",
    }


class Scripted:
    """Test double provider: returns pre-recorded model outputs in order."""

    name = "scripted"
    model = "recorded"
    real = True  # behaves like a real provider for the rules under test
    context_tokens = None

    def __init__(self, *outputs: str, context_tokens: int | None = None):
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []
        self.context_tokens = context_tokens

    def check(self):
        raise AssertionError("pick_clips must not need check(); tasks call it")

    def complete_json(self, system, user, schema, log, *, max_output_tokens=4096, images=None):
        assert schema is pick.OUTPUT_SCHEMA
        self.calls.append((system, user))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _run(client, n=8, t=None):
    return pick.pick_clips(t or _transcript(), "S1", client, lambda m: None, n=n, max_retries=2)


def test_valid_output_is_accepted_and_prompt_is_the_spec_prompt():
    client = Scripted(json.dumps({"clips": [_clip([(100, 122)])]}))
    res = _run(client)
    assert len(res.clips) == 1
    c = res.clips[0]
    assert c.rank == 1 and c.duration == 22 and not c.flags
    system, user = client.calls[0]
    assert system.startswith("You are a short-form video editor and viral strategist")
    assert "Never exceed 30 s." in system
    assert "HOST (S1)" in user and "GUEST (S2)" in user
    # Segment text comes from the transcript, not the model.
    assert c.plan.segments[0].text.startswith("w200")


def test_invalid_json_is_retried_then_fails_loudly():
    good = json.dumps({"clips": [_clip([(100, 120)])]})
    assert _run(Scripted("not json", good)).attempts == 2
    with pytest.raises(pick.PickerError, match="invalid JSON every time"):
        _run(Scripted("nope", "{}", '{"clips": [{"hook": 1}]}'))


def test_schema_violations_are_rejected():
    bad_score = _clip([(100, 120)], score=11)
    two_reasons = {**_clip([(100, 120)]), "reasons": ["only", "two"]}
    with pytest.raises(pick.PickerError):
        _run(Scripted(*[json.dumps({"clips": [bad_score]})] * 3))
    with pytest.raises(pick.PickerError):
        _run(Scripted(*[json.dumps({"clips": [two_reasons]})] * 3))


def test_no_clip_over_30_seconds_is_ever_accepted():
    out = {
        "clips": [
            _clip([(10, 41)]),  # 31 s single window
            _clip([(50, 66), (80, 95)]),  # stitched 16 + 15 = 31 s
            _clip([(150, 175)]),  # 25 s: fine
        ]
    }
    res = _run(Scripted(json.dumps(out)))
    assert [c.duration for c in res.clips] == [25]
    assert len(res.rejected) == 2
    assert all("30 s hard max" in r["reason"] for r in res.rejected)
    qa = pick.pick_qa(res, 8, _transcript())
    assert qa.passed


def test_out_of_range_lengths_are_flagged_not_dropped():
    res = _run(Scripted(json.dumps({"clips": [_clip([(100, 112)])]})))
    assert res.clips[0].flags and "outside 18-30" in res.clips[0].flags[0]


def test_never_padded_and_capped_at_n():
    three = {"clips": [_clip([(s, s + 20)]) for s in (100, 150, 200)]}
    res = _run(Scripted(json.dumps(three)), n=8)
    assert len(res.clips) == 3
    qa = pick.pick_qa(res, 8, _transcript())
    assert "fewer good clips than asked" in next(
        c.detail for c in qa.checks if c.name == "never padded beyond N"
    )
    res = _run(Scripted(json.dumps(three)), n=2)
    assert len(res.clips) == 2
    assert "more than N=2" in res.rejected[0]["reason"]


def test_overlapping_duplicates_and_bad_windows_are_rejected():
    out = {
        "clips": [
            _clip([(100, 120)]),
            _clip([(102, 121)]),  # same moment
            _clip([(320, 340)]),  # beyond the 300 s episode
            _clip([(60, 50)]),  # end before start
        ]
    }
    res = _run(Scripted(json.dumps(out)))
    assert len(res.clips) == 1
    reasons = " ".join(r["reason"] for r in res.rejected)
    assert "same moment" in reasons and "outside the episode" in reasons
    assert "end <= start" in reasons


def test_profanity_missed_by_the_model_is_flagged_with_its_time():
    res = _run(Scripted(json.dumps({"clips": [_clip([(15, 35)])]})))
    flags = res.clips[0].plan.risk_flags
    assert any(f.type == "profanity" and f.at_seconds == 20.0 for f in flags)


def test_profanity_already_flagged_is_not_duplicated():
    risk = [{"type": "profanity", "detail": "fucking", "at_seconds": 20.0}]
    res = _run(Scripted(json.dumps({"clips": [_clip([(15, 35)], risk=risk)]})))
    assert sum(f.type == "profanity" for f in res.clips[0].plan.risk_flags) == 1


def test_censored_spelling_counts_as_profanity():
    assert pick._is_profane("f**k") and pick._is_profane("Shit,")
    assert not pick._is_profane("shirt") and not pick._is_profane("class")


def test_bad_opener_is_flagged():
    t = _transcript()
    t.segments[10].words[0].text = "And"
    res = _run(Scripted(json.dumps({"clips": [_clip([(100, 120)])]})), t=t)
    assert any('opens on "and"' in f for f in res.clips[0].flags)


def test_test_double_marks_qa_as_not_real():
    t = _transcript()
    res = pick.pick_clips(t, "S1", FakeLLM(), lambda m: None, n=3)
    assert 0 < len(res.clips) <= 3
    assert res.real is False and res.provider == "fake"
    qa = pick.pick_qa(res, 3, t)
    assert not qa.passed and qa.failures[0].name == "real clip picking"


def test_provider_errors_fail_without_retrying():
    client = Scripted(LLMError("Ollama isn't reachable"), json.dumps({"clips": []}))
    with pytest.raises(pick.PickerError, match="isn't reachable"):
        _run(client)
    assert len(client.calls) == 1


def test_long_transcript_is_picked_in_overlapping_parts_that_fit_the_context():
    t = _transcript(1200)  # 20 minutes
    whole = len(pick.format_transcript(t, "S1")) // 3
    ctx = pick.PICKER_OUTPUT_TOKENS + 3000  # far smaller than the whole transcript
    assert whole > 3000
    windows = pick.plan_windows(t, "S1", 2000)
    assert len(windows) > 1
    covered = {s.start for w in windows for s in w}
    assert covered == {s.start for s in t.segments}  # nothing dropped
    for a, b in zip(windows, windows[1:], strict=False):
        assert b[0].start < a[-1].end  # neighbours overlap
        assert a[-1].end - b[0].start <= pick.WINDOW_OVERLAP_SECONDS + 10
    # One answer per part: later parts propose stronger clips, and part 1 also proposes a clip
    # outside its own time range (dropped).
    ranges = []

    class Recorder(Scripted):
        def complete_json(self, system, user, schema, log, **kw):
            assert len(system) // 3 + len(user) // 3 + kw["max_output_tokens"] <= ctx
            times = [float(x) for x in re.findall(r"^\[(\d+\.\d+)-", user, re.M)]
            lo = min(times)
            ranges.append(lo)
            i = len(ranges)
            clips = [_clip([(lo + 1, lo + 21)], score=5 + i)]
            if i == 1:
                clips.append(_clip([(1100, 1120)], score=9))  # not in part 1
            return json.dumps({"clips": clips})

    rec = Recorder(context_tokens=ctx)
    res = pick.pick_clips(t, "S1", rec, lambda m: None, n=8)
    assert res.windows == len(ranges) > 1
    scores = [c.plan.virality_score for c in res.clips]
    assert scores == sorted(scores, reverse=True)
    assert all(c.duration <= 30 for c in res.clips)
    assert not any(c.plan.start >= 1100 for c in res.clips)


def test_too_small_context_fails_clearly():
    with pytest.raises(pick.PickerError, match="OLLAMA_NUM_CTX"):
        _run(Scripted(context_tokens=pick.PICKER_OUTPUT_TOKENS + 900), t=_transcript(1200))


def test_output_schema_is_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                walk(v)
        if node.get("type") == "array":
            walk(node["items"])

    walk(pick.OUTPUT_SCHEMA)


def test_unknown_prompt_version():
    with pytest.raises(pick.PickerError, match="not found"):
        pick.load_prompt("v999")
