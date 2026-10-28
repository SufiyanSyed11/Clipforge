"""Search queries for each beat: what real footage would show this line.

Asks the configured language model (local by default) for beat-specific queries and any
named person/event, validated against a schema. If the model isn't available, a keyword
fallback is used and labelled as such.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from clipforge.core.captions import STOPWORDS
from clipforge.editspec import BrollSlot
from clipforge.llm.base import LLMError, LLMProvider

SYSTEM = """You are a documentary B-roll researcher for short vertical podcast clips.
For each numbered beat, write 3 short stock-footage search queries (2-5 words each) that
describe real footage matching what is said in that beat. Prefer concrete, filmable things
(places, objects, actions, sports moments). If the beat is about a specific real person or a
specific real event, name them exactly in "named_person"/"event"; otherwise use null.
Never suggest footage of the podcast itself. Return JSON only."""

SCHEMA = {
    "title": "broll_queries",
    "type": "object",
    "additionalProperties": False,
    "required": ["beats"],
    "properties": {
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "queries", "named_person", "event"],
                "properties": {
                    "index": {"type": "integer"},
                    "queries": {"type": "array", "items": {"type": "string"}},
                    "named_person": {"type": ["string", "null"]},
                    "event": {"type": ["string", "null"]},
                },
            },
        }
    },
}


class BeatQueries(BaseModel):
    index: int
    queries: list[str] = Field(min_length=1, max_length=5)
    named_person: str | None = None
    event: str | None = None


class QueryOutput(BaseModel):
    beats: list[BeatQueries]


def keyword_queries(text: str) -> list[str]:
    words = [re.sub(r"[^\w'-]", "", w) for w in text.split()]
    content = [w for w in words if len(w) > 3 and w.lower() not in STOPWORDS]
    names = [w for w in content if w[:1].isupper()]
    picks = (names + [w.lower() for w in content if w not in names])[:4]
    if not picks:
        return []
    return [" ".join(picks[:2]), " ".join(picks[:3]), picks[0]]


def generate(
    slots: list[BrollSlot],
    context: str,
    hints: list[str],
    provider: LLMProvider | None,
    log,
) -> tuple[list[BrollSlot], str]:
    """Fill `queries` / `named_person` on each slot. Returns (slots, method)."""
    if not slots:
        return slots, "none"
    if provider is not None:
        beats = "\n".join(f"{s.index}. {s.beat_text}" for s in slots)
        user = (
            f"Clip context: {context}\n"
            + (f"Editor's B-roll ideas: {'; '.join(hints)}\n" if hints else "")
            + f"\nBeats:\n{beats}"
        )
        try:
            raw = provider.complete_json(SYSTEM, user, SCHEMA, log, max_output_tokens=2000)
            out = QueryOutput.model_validate_json(raw)
            by_index = {b.index: b for b in out.beats}
            filled = []
            for s in slots:
                b = by_index.get(s.index)
                queries = [q.strip() for q in (b.queries if b else []) if q.strip()][:3]
                filled.append(
                    s.model_copy(
                        update={
                            "queries": queries or keyword_queries(s.beat_text),
                            "named_person": (b.named_person or None) if b else None,
                        }
                    )
                )
            return filled, f"{provider.name}:{provider.model}"
        except (LLMError, ValidationError, ValueError, json.JSONDecodeError) as e:
            log(f"B-roll query generation failed ({e}); using keyword fallback")
    return [s.model_copy(update={"queries": keyword_queries(s.beat_text)}) for s in slots], (
        "keyword fallback"
    )
