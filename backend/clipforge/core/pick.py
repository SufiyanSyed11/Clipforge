"""Clip picking with a language model (local Ollama by default), validated and held to the
hard rules in code.

The model proposes; this module enforces: schema validation (retry, then fail loudly), the
30 s hard max (over-long clips are rejected, never trimmed silently), the 18-30 s window
(flagged), no padding, profanity flags with times, and overlap de-duplication.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from pydantic import BaseModel, ValidationError

from clipforge.llm.base import LLMError, LLMProvider, estimate_tokens
from clipforge.models import ClipPlan, PickedClip, RiskFlag, Segment, Transcript
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


def format_transcript(
    t: Transcript, host_speaker: str | None, segments: list[Segment] | None = None
) -> str:
    def label(sp: str | None) -> str:
        if not sp:
            return "SPEAKER"
        if host_speaker:
            return f"HOST ({sp})" if sp == host_speaker else f"GUEST ({sp})"
        return sp

    return "\n".join(
        f"[{s.start:.2f}-{s.end:.2f}] {label(s.speaker)}: {s.text}"
        for s in (t.segments if segments is None else segments)
    )


def build_user_message(
    t: Transcript,
    host_speaker: str | None,
    n: int,
    style: dict,
    segments: list[Segment] | None = None,
    extra_context: str = "",
) -> str:
    part = ""
    if segments is not None and segments:
        part = (
            f"This is one part of the episode ({segments[0].start:.0f}-{segments[-1].end:.0f} s); "
            "pick only from it.\n"
        )
    return (
        f"N = {n}\n"
        f"Clip length: {style.get('min_clip_seconds', 18)}-"
        f"{min(style.get('max_clip_seconds', 30), HARD_MAX_SECONDS)} s, hard max 30 s.\n"
        f"Episode length: {t.duration:.1f} s. Language: {t.language or 'unknown'}.\n"
        f"{part}"
        + (f"\n{extra_context.strip()}\n" if extra_context.strip() else "")
        + f"\n<transcript>\n{format_transcript(t, host_speaker, segments)}\n</transcript>"
    )


# ---- rules ----------------------------------------------------------------------------------


@dataclass
class PickResult:
    clips: list[PickedClip]
    rejected: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    raw: str = ""
    provider: str = ""
    model: str = ""
    real: bool = True
    windows: int = 1


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


# Output budget for one picker answer: 8 clips of ~350 tokens each, with headroom.
PICKER_OUTPUT_TOKENS = 6000
# Seconds of transcript repeated between neighbouring windows, so a story that crosses a
# window boundary is still whole in one of them.
WINDOW_OVERLAP_SECONDS = 60.0


def plan_windows(
    t: Transcript, host_speaker: str | None, budget_tokens: int | None
) -> list[list[Segment]]:
    """Split the transcript into windows whose formatted text fits `budget_tokens`."""
    segs = t.segments
    whole = estimate_tokens(format_transcript(t, host_speaker))
    if budget_tokens is None or whole <= budget_tokens:
        return [segs]
    if budget_tokens < 500:
        raise PickerError(
            "The model's context is too small for clip picking; raise OLLAMA_NUM_CTX."
        )
    windows: list[list[Segment]] = []
    i = 0
    while i < len(segs):
        window: list[Segment] = []
        used = 0
        j = i
        while j < len(segs):
            cost = estimate_tokens(format_transcript(t, host_speaker, [segs[j]])) + 1
            if window and used + cost > budget_tokens:
                break
            window.append(segs[j])
            used += cost
            j += 1
        windows.append(window)
        if j >= len(segs):
            break
        # Step back so the next window starts ~WINDOW_OVERLAP_SECONDS before this one ended.
        overlap_from = window[-1].end - WINDOW_OVERLAP_SECONDS
        k = j
        while k - 1 > i + 1 and segs[k - 1].start > overlap_from:
            k -= 1
        i = k
    return windows


def _ask(
    provider: LLMProvider,
    system: str,
    user: str,
    log: Logger,
    max_retries: int,
    label: str,
) -> tuple[PickOutput, int, str]:
    errors: list[str] = []
    for attempt in range(1, max_retries + 2):
        log(f"Asking {provider.name}:{provider.model} for clips ({label}, attempt {attempt})")
        try:
            text = provider.complete_json(
                system, user, OUTPUT_SCHEMA, log, max_output_tokens=PICKER_OUTPUT_TOKENS
            )
        except LLMError as e:
            # Unavailable/out-of-memory/etc.: retrying the same call won't help.
            raise PickerError(str(e)) from e
        try:
            return _parse(text), attempt, text
        except (ValidationError, ValueError) as e:
            err = str(e).splitlines()[0] if str(e) else type(e).__name__
            errors.append(f"attempt {attempt}: {err}")
            log(f"Picker output failed validation: {err}")
    raise PickerError(
        f"Clip picker returned invalid JSON every time ({label}): " + "; ".join(errors)
    )


def pick_clips(
    t: Transcript,
    host_speaker: str | None,
    provider: LLMProvider,
    log: Logger,
    n: int = 8,
    style: dict | None = None,
    prompt_version: str = "v1",
    max_retries: int = 2,
    extra_context: str = "",
) -> PickResult:
    style = style or {}
    system = load_prompt(prompt_version)
    budget = None
    if provider.context_tokens:
        header = build_user_message(t, host_speaker, n, style, [], extra_context)
        budget = (
            provider.context_tokens
            - PICKER_OUTPUT_TOKENS
            - estimate_tokens(system)
            - estimate_tokens(header)
            - 200
        )
    windows = plan_windows(t, host_speaker, budget)
    if len(windows) > 1:
        log(
            f"Transcript is longer than the model's {provider.context_tokens}-token context; "
            f"picking in {len(windows)} overlapping parts"
        )
    plans: list[ClipPlan] = []
    attempts = 0
    raws: list[str] = []
    for w_i, segs in enumerate(windows, 1):
        user = build_user_message(
            t, host_speaker, n, style, segs if len(windows) > 1 else None, extra_context
        )
        label = f"part {w_i}/{len(windows)}"
        out, used, raw = _ask(provider, system, user, log, max_retries, label)
        attempts = max(attempts, used)
        raws.append(raw)
        if len(windows) > 1:
            lo, hi = segs[0].start - 0.5, segs[-1].end + 0.5
            out_of_part = [
                c for c in out.clips if c.start < lo or max(s.end for s in c.segments) > hi
            ]
            for _ in out_of_part:
                log(f"Part {w_i}: dropped a clip outside this part's time range")
            plans.extend(c for c in out.clips if c not in out_of_part)
        else:
            plans.extend(out.clips)
    if len(windows) > 1:
        # Parts were ranked separately; rank the union by score (stable: earlier parts win ties).
        plans.sort(key=lambda c: -c.virality_score)
    clips, rejected = apply_rules(
        plans,
        t,
        n,
        min_s=style.get("min_clip_seconds", 18.0),
        max_s=style.get("max_clip_seconds", HARD_MAX_SECONDS),
    )
    for r in rejected:
        log(f"Rejected model clip #{r['model_rank']}: {r['reason']}")
    return PickResult(
        clips=clips,
        rejected=rejected,
        attempts=attempts,
        raw="\n".join(raws),
        provider=provider.name,
        model=provider.model,
        real=provider.real,
        windows=len(windows),
    )


def pick_qa(result: PickResult, n: int, transcript: Transcript) -> QAReport:
    qa = QAReport(step="pick")
    if transcript.fake or not result.real:
        qa.check("real clip picking", False, "FAKE_MODELS test double: plumbing test only")
    qa.check(
        "output matched the schema",
        True,
        f"{result.provider}:{result.model}, after {result.attempts} attempt(s)"
        + (f", {result.windows} transcript parts" if result.windows > 1 else ""),
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
