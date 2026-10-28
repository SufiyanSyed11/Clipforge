"""P1-6: clip picker validation and hard rules (recorded/fake model responses, no API)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clipforge.core import pick
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
    """Returns pre-recorded model outputs in order."""

    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, log):
        self.calls.append((system, user))
        return self.outputs.pop(0)


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


def test_fake_client_marks_qa_as_not_real():
    t = _transcript().model_copy(update={"fake": True})
    res = pick.pick_clips(t, "S1", pick.FakePickerClient(t), lambda m: None, n=3)
    assert 0 < len(res.clips) <= 3
    qa = pick.pick_qa(res, 3, t)
    assert not qa.passed and qa.failures[0].name == "real clip picking"


# ---- the real SDK call path, against a stand-in client object ------------------------------


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def _sdk_client(msg, record):
    def stream(**kw):
        record.append(kw)
        return _Stream(msg)

    return SimpleNamespace(
        messages=SimpleNamespace(stream=stream),
        beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)),
    )


def _msg(text, stop="end_turn", model="claude-opus-5-5"):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=None,
        model=model,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )


def test_claude_client_request_shape():
    calls: list[dict] = []
    c = pick.ClaudePickerClient(
        "claude-opus-5-5", "high", "default", client=_sdk_client(_msg('{"clips": []}'), calls)
    )
    assert c.complete("sys", "user", lambda m: None) == '{"clips": []}'
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "high"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw

    calls.clear()
    pick.ClaudePickerClient(
        "claude-opus-5-5", fallbacks="off", client=_sdk_client(_msg("{}"), calls)
    ).complete("s", "u", lambda m: None)
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


@pytest.mark.parametrize(("stop", "match"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_claude_client_stop_reasons_fail_loudly(stop, match):
    c = pick.ClaudePickerClient("m", client=_sdk_client(_msg("", stop=stop), []))
    with pytest.raises(pick.PickerError, match=match):
        c.complete("s", "u", lambda m: None)


def test_claude_client_logs_fallback_model():
    logs: list[str] = []
    c = pick.ClaudePickerClient(
        "claude-opus-5-5", client=_sdk_client(_msg("{}", model="other-model"), [])
    )
    c.complete("s", "u", logs.append)
    assert any("fallback model other-model" in m for m in logs)


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
