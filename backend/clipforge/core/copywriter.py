"""Copy pack per clip: title, YouTube description, Instagram caption + tags, X post.

The language model (local by default) drafts; code enforces the rules from the spec:
- title: curiosity gap, named person, quoted punchline, exactly one emoji, <= 100 chars;
- YouTube description with emojis and a call to comment;
- Instagram caption with the brand tag plus related tags, max 5 tags in total;
- X post: 1-2 sentences, no emojis, <= 280 chars;
- no credit line unless the owner set one.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ValidationError

from clipforge.llm.base import LLMError, LLMProvider
from clipforge.qa import QAReport

MAX_TAGS = 5

SCHEMA = {
    "title": "copy_pack",
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "youtube_description", "instagram_caption", "instagram_tags", "x_post"],
    "properties": {
        "title": {"type": "string"},
        "youtube_description": {"type": "string"},
        "instagram_caption": {"type": "string"},
        "instagram_tags": {"type": "array", "items": {"type": "string"}},
        "x_post": {"type": "string"},
    },
}

_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002b00-\U00002bff"
    "\U0001f900-\U0001f9ff←-⇿⌀-⏿⭐⭕〰〽㊗㊙]"
)


def emojis(text: str) -> list[str]:
    return _EMOJI.findall(text)


class CopyPack(BaseModel):
    title: str
    youtube_description: str
    instagram_caption: str
    instagram_tags: list[str]
    x_post: str
    credit_line: str = ""
    generated_by: str = ""
    edited: bool = False


class CopyInvalid(Exception):
    pass


def _tag(t: str) -> str:
    t = re.sub(r"[^\w]", "", t.lstrip("#"))
    return f"#{t}" if t else ""


def normalize(raw: dict, brand_tag: str, credit_line: str) -> CopyPack:
    """Enforce the rules; raise CopyInvalid for anything that can't be fixed mechanically."""
    title = re.sub(r"\s+", " ", raw["title"]).strip()
    n_emoji = len(emojis(title))
    if n_emoji != 1:
        raise CopyInvalid(f"title must have exactly one emoji (has {n_emoji})")
    if len(title) > 100:
        raise CopyInvalid("title is over YouTube's 100-character limit")
    x = re.sub(r"\s+", " ", raw["x_post"]).strip()
    if emojis(x):
        raise CopyInvalid("X post must not contain emojis")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", x) if s.strip()]
    if not 1 <= len(sentences) <= 2:
        raise CopyInvalid(f"X post must be 1-2 sentences (has {len(sentences)})")
    if len(x) > 280:
        raise CopyInvalid("X post is over 280 characters")
    yt = raw["youtube_description"].strip()
    if not emojis(yt):
        raise CopyInvalid("YouTube description needs emojis")
    if not re.search(r"comment|let me know|tell us|\?", yt, re.I):
        raise CopyInvalid("YouTube description needs a call to comment")
    # Tags: brand first, then related, de-duplicated, max 5 in total.
    brand = _tag(brand_tag)
    tags = [brand] if brand else []
    for t in raw["instagram_tags"]:
        tt = _tag(t)
        if tt and tt.lower() not in {x.lower() for x in tags}:
            tags.append(tt)
    tags = tags[:MAX_TAGS]
    # Hashtags written inside the caption text count too: move them to the tag list.
    caption = re.sub(r"(?:^|\s)#\w+", "", raw["instagram_caption"]).strip()
    # No credit line unless the owner set one.
    if not credit_line:
        caption = re.sub(r"(?im)^\s*(credit|credits|via|source)\s*:.*$", "", caption).strip()
        yt = re.sub(r"(?im)^\s*(credit|credits|via|source)\s*:.*$", "", yt).strip()
    else:
        yt = f"{yt}\n\n{credit_line}"
        caption = f"{caption}\n\n{credit_line}"
    return CopyPack(
        title=title,
        youtube_description=yt,
        instagram_caption=caption,
        instagram_tags=tags,
        x_post=x,
        credit_line=credit_line,
    )


SYSTEM = """You write social copy for short vertical podcast clips for a sports/culture
channel. Rules:
- title: a curiosity gap, the named person if there is one, a quoted punchline, and exactly
  ONE emoji. Under 90 characters.
- youtube_description: 2-4 short lines with a few emojis, ending with a question that invites
  people to comment.
- instagram_caption: 1-3 punchy lines, no hashtags inside the text.
- instagram_tags: up to 4 related hashtags (the brand tag is added separately).
- x_post: 1-2 plain sentences, NO emojis, no hashtags.
- Never add a credit line. Never invent facts that aren't in the clip; for claims about real
  people, attribute them ("he says").
Return JSON only."""


def build_user(
    transcript_text: str,
    hook: str,
    payoff: str,
    title_ideas: list[str],
    brand_tag: str,
    learning: str = "",
    feedback: str = "",
) -> str:
    return (
        f"Clip transcript: {transcript_text}\n"
        f"Hook: {hook}\nPayoff: {payoff}\n"
        f"Title ideas from the clip picker: {'; '.join(title_ideas)}\n"
        f"Brand tag (added automatically): {brand_tag}\n"
        + (f"\nWhat has worked on this channel recently:\n{learning}\n" if learning else "")
        + (f"\nYour previous answer broke a rule: {feedback}. Fix it.\n" if feedback else "")
    )


def write_copy(
    provider: LLMProvider,
    transcript_text: str,
    hook: str,
    payoff: str,
    title_ideas: list[str],
    brand_tag: str,
    credit_line: str,
    log,
    learning: str = "",
    max_retries: int = 2,
) -> tuple[CopyPack, QAReport]:
    feedback = ""
    errors = []
    for attempt in range(1, max_retries + 2):
        user = build_user(transcript_text, hook, payoff, title_ideas, brand_tag, learning, feedback)
        try:
            raw = provider.complete_json(SYSTEM, user, SCHEMA, log, max_output_tokens=1200)
        except LLMError as e:
            raise CopyInvalid(f"copy model unavailable: {e}") from e
        try:
            import json

            pack = normalize(json.loads(raw), brand_tag, credit_line)
        except (CopyInvalid, ValidationError, ValueError, KeyError, TypeError) as e:
            feedback = str(e)
            errors.append(f"attempt {attempt}: {e}")
            log(f"Copy rejected: {e}")
            continue
        pack.generated_by = f"{provider.name}:{provider.model}"
        return pack, copy_qa(pack, brand_tag, attempt, provider.real)
    raise CopyInvalid("Copy broke the rules every time: " + "; ".join(errors))


def copy_qa(pack: CopyPack, brand_tag: str, attempts: int = 1, real: bool = True) -> QAReport:
    qa = QAReport(step="copy")
    if not real:
        qa.check("real copy writing", False, "TEST DOUBLE copy: not real output")
    qa.check("title has exactly one emoji", len(emojis(pack.title)) == 1, pack.title)
    qa.check("title within 100 characters", len(pack.title) <= 100, f"{len(pack.title)} chars")
    qa.check(
        "Instagram: brand tag + related, max 5",
        len(pack.instagram_tags) <= MAX_TAGS
        and (not brand_tag or pack.instagram_tags[:1] == [_tag(brand_tag)]),
        " ".join(pack.instagram_tags),
    )
    qa.check("X post has no emojis", not emojis(pack.x_post), f"{len(pack.x_post)} chars")
    qa.check(
        "YouTube description has a call to comment",
        bool(re.search(r"comment|let me know|tell us|\?", pack.youtube_description, re.I)),
    )
    qa.check(
        "no credit line unless set",
        bool(pack.credit_line)
        or not re.search(r"(?im)^\s*(credit|credits|via|source)\s*:", pack.instagram_caption),
    )
    if attempts > 1:
        qa.check("copy needed retries", True, f"{attempts} attempts", severity="warn")
    if pack.edited:
        qa.check("edited by the owner", True, severity="warn")
    return qa
