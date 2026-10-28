"""Clip picking with the Claude API, validated and held to the hard rules in code.

The model proposes; this module enforces: schema validation (retry, then fail loudly), the
30 s hard max (over-long clips are rejected, never trimmed silently), the 18-30 s window
(flagged), no padding, profanity flags with times, and overlap de-duplication.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from clipforge.models import ClipPlan, PickedClip, RiskFlag, Transcript
from clipforge.qa import QAReport

HARD_MAX_SECONDS = 30.0
Logger = Callable[[str], None]

BAD_OPENERS = {"and", "but", "so", "that's", "thats", "he", "she", "they", "it", "because"}
# Common profanity; matched on whole words, case-insensitive, with * censoring variants.
PROFANITY = {
    "fuck",
    "fucking",
    "fucked",
    "fucker",
    "motherfucker",
    "shit",
    "shitty",
    "bullshit",
    "bitch",
    "bitches",
    "asshole",
    "dick",
    "cunt",
    "bastard",
    "damn",
    "goddamn",
    "piss",
    "pissed",
    "crap",
}


class PickerError(Exception):
    pass


class PickOutput(BaseModel):
    clips: list[ClipPlan]


# JSON schema for structured output. Count and range limits (3 reasons, score 1-10) are
# enforced by pydantic after parsing, since structured outputs don't support every keyword.
_STR = {"type": "string"}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clips"],
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "segments",
                    "hook",
                    "context",
                    "payoff",
                    "virality_score",
                    "reasons",
                    "title_options",
                    "broll_beats",
                    "risk_flags",
                    "improvements",
                ],
                "properties": {
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start", "end", "text"],
                            "properties": {
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                                "text": _STR,
                            },
                        },
                    },
                    "hook": _STR,
                    "context": _STR,
                    "payoff": _STR,
                    "virality_score": {"type": "integer"},
                    "reasons": {"type": "array", "items": _STR},
                    "title_options": {"type": "array", "items": _STR},
                    "broll_beats": {"type": "array", "items": _STR},
                    "risk_flags": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "detail", "at_seconds"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "profanity",
                                        "unverifiable_claim",
                                        "private_info",
                                        "defamation",
                                        "other",
                                    ],
                                },
                                "detail": _STR,
                                "at_seconds": {"type": ["number", "null"]},
                            },
                        },
                    },
                    "improvements": _STR,
                },
            },
        }
    },
}


def load_prompt(version: str) -> str:
    name = f"clip_picker.{version}.md"
    try:
        return resources.files("clipforge.prompts").joinpath(name).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PickerError(f"Prompt version '{version}' not found ({name})") from e


def format_transcript(t: Transcript, host_speaker: str | None) -> str:
    def label(sp: str | None) -> str:
        if not sp:
            return "SPEAKER"
        if host_speaker:
            return f"HOST ({sp})" if sp == host_speaker else f"GUEST ({sp})"
        return sp

    return "\n".join(
        f"[{s.start:.2f}-{s.end:.2f}] {label(s.speaker)}: {s.text}" for s in t.segments
    )


def build_user_message(t: Transcript, host_speaker: str | None, n: int, style: dict) -> str:
    return (
        f"N = {n}\n"
        f"Clip length: {style.get('min_clip_seconds', 18)}-"
        f"{min(style.get('max_clip_seconds', 30), HARD_MAX_SECONDS)} s, hard max 30 s.\n"
        f"Episode length: {t.duration:.1f} s. Language: {t.language or 'unknown'}.\n\n"
        f"<transcript>\n{format_transcript(t, host_speaker)}\n</transcript>"
    )


# ---- model clients --------------------------------------------------------------------------


class PickerClient(Protocol):
    def complete(self, system: str, user: str, log: Logger) -> str:
        """Return the model's JSON text. Raise PickerError on refusal/truncation."""
        ...


class ClaudePickerClient:
    def __init__(
        self,
        model: str,
        effort: str = "high",
        fallbacks: str = "default",
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.use_fallbacks = fallbacks == "default"

    def _request(self, system: str, user: str, with_fallbacks: bool):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=64000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            messages=[{"role": "user", "content": user}],
        )
        if with_fallbacks:
            # A request the model declines is re-run on a fallback model in the same call.
            stream = self.client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            stream = self.client.messages.stream(**kwargs)
        with stream as s:
            return s.get_final_message()

    def complete(self, system: str, user: str, log: Logger) -> str:
        import anthropic

        try:
            msg = self._request(system, user, self.use_fallbacks)
        except anthropic.BadRequestError as e:
            if not self.use_fallbacks or "fallback" not in str(e).lower():
                raise PickerError(f"Claude API rejected the request: {e}") from e
            log("Server-side fallback not accepted for this request; retrying without it")
            msg = self._request(system, user, False)
        except anthropic.AuthenticationError as e:
            raise PickerError("Claude API key missing or invalid (ANTHROPIC_API_KEY)") from e
        except anthropic.RateLimitError as e:
            raise PickerError(f"Claude API rate limit: {e}") from e
        except anthropic.APIStatusError as e:
            raise PickerError(f"Claude API error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise PickerError(f"Can't reach the Claude API: {e}") from e

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise PickerError(f"The model declined this transcript ({details})")
        if msg.stop_reason == "max_tokens":
            raise PickerError("The model's answer was cut off (max_tokens)")
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        served_by = getattr(msg, "model", self.model)
        if served_by and served_by != self.model:
            log(f"Answered by fallback model {served_by}")
        return text


class FakePickerClient:
    """Test stub (FAKE_MODELS): picks consecutive transcript windows of ~20 s."""

    def __init__(self, transcript: Transcript) -> None:
        self.t = transcript

    def complete(self, system: str, user: str, log: Logger) -> str:
        n = int(re.search(r"N = (\d+)", user).group(1))
        clips = []
        segs = self.t.segments
        i = 0
        while i < len(segs) and len(clips) < n:
            start = segs[i].start
            j = i
            while j + 1 < len(segs) and segs[j + 1].end - start <= 24:
                j += 1
            if segs[j].end - start >= 18:
                text = " ".join(s.text for s in segs[i : j + 1])
                clips.append(
                    {
                        "segments": [{"start": start, "end": segs[j].end, "text": text}],
                        "hook": segs[i].text,
                        "context": segs[min(i + 1, j)].text,
                        "payoff": segs[j].text,
                        "virality_score": max(1, 8 - len(clips)),
                        "reasons": ["stub reason 1", "stub reason 2", "stub reason 3"],
                        "title_options": ["Stub title A 🔥", '"Stub quote" 😮', "Stub C 🎯"],
                        "broll_beats": ["stub beat"],
                        "risk_flags": [],
                        "improvements": "stub",
                    }
                )
            i = j + 1
        return json.dumps({"clips": clips})


# ---- rules ----------------------------------------------------------------------------------


@dataclass
class PickResult:
    clips: list[PickedClip]
    rejected: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    raw: str = ""


def _parse(text: str) -> PickOutput:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return PickOutput.model_validate_json(text)


def _overlap(a: ClipPlan, b: ClipPlan) -> float:
    """Shared seconds between two clips as a share of the shorter one."""
    shared = sum(
        max(0.0, min(x.end, y.end) - max(x.start, y.start)) for x in a.segments for y in b.segments
    )
    return shared / max(min(a.duration, b.duration), 1e-6)


def _norm(word: str) -> str:
    return re.sub(r"[^a-z*']", "", word.lower())


def _is_profane(word: str) -> bool:
    w = _norm(word).strip("'")
    if w in PROFANITY:
        return True
    # Censored spellings from the transcript (f**k, sh*t).
    return "*" in w and any(
        len(p) == len(w) and all(c == "*" or c == pc for c, pc in zip(w, p, strict=True))
        for p in PROFANITY
    )


def apply_rules(
    plans: list[ClipPlan],
    t: Transcript,
    n: int,
    min_s: float = 18.0,
    max_s: float = HARD_MAX_SECONDS,
) -> tuple[list[PickedClip], list[dict[str, Any]]]:
    max_s = min(max_s, HARD_MAX_SECONDS)
    accepted: list[PickedClip] = []
    rejected: list[dict[str, Any]] = []
    for idx, plan in enumerate(plans):
        segs = sorted(plan.segments, key=lambda s: s.start)
        problem = None
        for s in segs:
            if s.end <= s.start:
                problem = f"segment {s.start:.2f}-{s.end:.2f} has end <= start"
            elif s.start < 0 or s.end > t.duration + 0.5:
                problem = f"segment {s.start:.2f}-{s.end:.2f} is outside the episode"
        for a, b in zip(segs, segs[1:], strict=False):
            if b.start < a.end:
                problem = f"segments overlap at {b.start:.2f}"
        plan = plan.model_copy(update={"segments": segs})
        if not problem and plan.duration > HARD_MAX_SECONDS:
            problem = f"{plan.duration:.1f} s is over the 30 s hard max"
        if not problem:
            for kept in accepted:
                if _overlap(plan, kept.plan) > 0.5:
                    problem = f"mostly the same moment as clip #{kept.rank}"
                    break
        if problem:
            rejected.append({"model_rank": idx + 1, "reason": problem, "plan": plan.model_dump()})
            continue
        if len(accepted) >= n:
            rejected.append({"model_rank": idx + 1, "reason": f"more than N={n} clips"})
            continue

        flags: list[str] = []
        if not (min_s <= plan.duration <= max_s):
            flags.append(f"duration {plan.duration:.1f} s is outside {min_s:.0f}-{max_s:.0f} s")
        # Replace model text with what the transcript actually says in each window.
        fixed_segments = []
        risk = list(plan.risk_flags)
        flagged_times = {round(r.at_seconds or -1, 1) for r in risk if r.type == "profanity"}
        for s in segs:
            words = t.words_between(s.start, s.end)
            if words:
                fixed_segments.append(
                    s.model_copy(update={"text": " ".join(w.text for w in words)})
                )
            else:
                fixed_segments.append(s)
                flags.append(f"no transcript words in {s.start:.2f}-{s.end:.2f}")
            for w in words:
                if _is_profane(w.text) and round(w.start, 1) not in flagged_times:
                    risk.append(
                        RiskFlag(
                            type="profanity",
                            detail=f'"{w.text}" (found by the profanity check)',
                            at_seconds=w.start,
                        )
                    )
                    flagged_times.add(round(w.start, 1))
        plan = plan.model_copy(update={"segments": fixed_segments, "risk_flags": risk})
        first = _norm(plan.segments[0].text.split()[0]) if plan.segments[0].text else ""
        if first in BAD_OPENERS:
            flags.append(f'opens on "{first}", which may depend on earlier talk')
        accepted.append(
            PickedClip(
                rank=len(accepted) + 1,
                plan=plan,
                duration=round(plan.duration, 2),
                stitched=len(plan.segments) > 1,
                flags=flags,
            )
        )
    return accepted, rejected


def pick_clips(
    t: Transcript,
    host_speaker: str | None,
    client: PickerClient,
    log: Logger,
    n: int = 8,
    style: dict | None = None,
    prompt_version: str = "v1",
    max_retries: int = 2,
) -> PickResult:
    style = style or {}
    system = load_prompt(prompt_version)
    user = build_user_message(t, host_speaker, n, style)
    errors: list[str] = []
    for attempt in range(1, max_retries + 2):
        log(f"Asking the clip picker (prompt {prompt_version}, attempt {attempt})")
        text = client.complete(system, user, log)
        try:
            out = _parse(text)
        except (ValidationError, ValueError) as e:
            err = str(e).splitlines()[0] if str(e) else type(e).__name__
            errors.append(f"attempt {attempt}: {err}")
            log(f"Picker output failed validation: {err}")
            continue
        clips, rejected = apply_rules(
            out.clips,
            t,
            n,
            min_s=style.get("min_clip_seconds", 18.0),
            max_s=style.get("max_clip_seconds", HARD_MAX_SECONDS),
        )
        for r in rejected:
            log(f"Rejected model clip #{r['model_rank']}: {r['reason']}")
        return PickResult(clips=clips, rejected=rejected, attempts=attempt, raw=text)
    raise PickerError("Clip picker returned invalid JSON every time: " + "; ".join(errors))


def pick_qa(result: PickResult, n: int, transcript: Transcript) -> QAReport:
    qa = QAReport(step="pick")
    if transcript.fake:
        qa.check("real clip picking", False, "FAKE_MODELS stub: plumbing test only")
    qa.check(
        "output matched the schema",
        True,
        f"after {result.attempts} attempt(s)",
    )
    qa.check(
        "no clip over the 30 s hard max",
        all(c.duration <= HARD_MAX_SECONDS for c in result.clips),
        f"longest {max((c.duration for c in result.clips), default=0):.1f} s",
    )
    qa.check(
        "never padded beyond N",
        len(result.clips) <= n,
        f"{len(result.clips)} clips for N={n}"
        + (" (fewer good clips than asked)" if len(result.clips) < n else ""),
    )
    qa.check("at least one clip found", bool(result.clips), severity="warn")
    for c in result.clips:
        qa.check(
            f"clip #{c.rank} length in range",
            not any(f.startswith("duration") for f in c.flags),
            f"{c.duration:.1f} s" + (" (stitched)" if c.stitched else ""),
            severity="warn",
            at_seconds=c.plan.start,
        )
        risky = [r for r in c.plan.risk_flags]
        if risky:
            qa.check(
                f"clip #{c.rank} risk flags",
                False,
                "; ".join(f"{r.type}: {r.detail}" for r in risky),
                severity="warn",
                at_seconds=c.plan.start,
            )
    if result.rejected:
        qa.check(
            "model clips rejected by the hard rules",
            True,
            "; ".join(f"#{r['model_rank']} {r['reason']}" for r in result.rejected),
            severity="warn",
        )
    qa.check(
        "cut points",
        True,
        "windows use whisper timestamps; Phase 2 moves every cut to real silence",
        severity="warn",
    )
    return qa
