"""Vision check of a contact sheet against the reject rules and the owner's banned list.

The model describes what it sees; the decision is made in code: any issue, any banned item,
a mismatch with the beat, or an unconfirmed named person means "no". Identity is never judged
from faces: a named person must be named in the footage's own title/tags, and the model only
confirms the footage is consistent with that (same sport/setting/event), not who someone is.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ValidationError

from clipforge.llm.base import LLMError, LLMProvider

SCHEMA = {
    "title": "broll_vision",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "description",
        "issues",
        "banned_seen",
        "people_visible",
        "matches_beat",
        "consistent_with_named_subject",
        "accept",
    ],
    "properties": {
        "description": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "banned_seen": {"type": "array", "items": {"type": "string"}},
        "people_visible": {"type": "integer"},
        "matches_beat": {"type": "boolean"},
        "consistent_with_named_subject": {"type": ["boolean", "null"]},
        "accept": {"type": "boolean"},
    },
}


class Verdict(BaseModel):
    description: str
    issues: list[str]
    banned_seen: list[str]
    people_visible: int
    matches_beat: bool
    consistent_with_named_subject: bool | None
    accept: bool


def rules_hash(reject_rules: list[str], banned: list[str], model: str) -> str:
    blob = json.dumps({"reject": reject_rules, "banned": banned, "model": model}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def system_prompt(reject_rules: list[str], banned: list[str]) -> str:
    return (
        "You check stock-footage contact sheets (6 frames from one shot, left to right, top "
        "to bottom) for a professional video editor. Be strict.\n"
        "List in `issues` every one of these you see in ANY frame: "
        + "; ".join(reject_rules)
        + ".\nList in `banned_seen` every item of the owner's banned-content list you see in "
        "ANY frame: " + ("; ".join(banned) if banned else "(none)") + ".\n"
        "`matches_beat`: true only if the footage plausibly shows what the spoken line is "
        "about. `consistent_with_named_subject`: null if no person/event is named; otherwise "
        "true only if the footage is consistent with the named subject's context (same "
        "sport, setting or event). Do not try to recognise who anyone is from their face.\n"
        "`accept`: true only if issues and banned_seen are both empty and matches_beat is true."
    )


def check(
    provider: LLMProvider,
    sheet_jpeg: bytes,
    beat_text: str,
    named_person: str | None,
    title: str,
    reject_rules: list[str],
    banned: list[str],
    log,
) -> tuple[bool, dict]:
    user = (
        f"Spoken line: {beat_text}\n"
        f"Named subject: {named_person or 'none'}\n"
        f"Footage title/tags: {title}"
    )
    try:
        raw = provider.complete_json(
            system_prompt(reject_rules, banned),
            user,
            SCHEMA,
            log,
            max_output_tokens=800,
            images=[sheet_jpeg],
        )
        v = Verdict.model_validate_json(raw)
    except (LLMError, ValidationError, ValueError) as e:
        return False, {"error": f"vision check failed: {e}"}
    ok = (
        v.accept
        and not v.issues
        and not v.banned_seen
        and v.matches_beat
        and (named_person is None or v.consistent_with_named_subject is True)
    )
    return ok, {**v.model_dump(), "model": f"{provider.name}:{provider.model}"}
