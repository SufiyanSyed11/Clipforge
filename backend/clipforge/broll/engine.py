"""Fill B-roll slots. A slot is filled only by footage that passed every check; otherwise it is
WEAK ("needs manual cover") with its candidates kept for the owner's review screen."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from clipforge.broll import media, vision
from clipforge.broll.sources import Candidate, Source
from clipforge.editspec import BrollChoice, BrollSlot
from clipforge.llm.base import LLMProvider
from clipforge.qa import QAReport

MAX_CANDIDATES = 4  # downloads per slot (quality over quantity; they're cached)


@dataclass
class BrollContext:
    sources: list[Source]
    vision: LLMProvider | None  # None: owner approves contact sheets
    reject_rules: list[str]
    banned: list[str]
    cache: Path
    sheets: Path
    podcast_hashes: list[int]
    podcast_url: str | None
    podcast_sha1: str | None
    log: Callable[[str], None] = lambda m: None
    cancelled: Callable[[], bool] = lambda: False
    # owner decisions by slot index: {"action": "approve"|"clear", "candidate": int}
    decisions: dict[str, dict] = field(default_factory=dict)
    # slots from the previous build, by beat text (reuse candidates instead of searching)
    previous: dict[str, BrollSlot] = field(default_factory=dict)
    transport: object | None = None

    @property
    def rules(self) -> str:
        model = f"{self.vision.name}:{self.vision.model}" if self.vision else "owner"
        return vision.rules_hash(self.reject_rules, self.banned, model)


def name_in_metadata(name: str, c: Candidate | BrollChoice) -> bool:
    text = " ".join([c.title, *getattr(c, "tags", [])]).lower()
    parts = [p for p in re.split(r"\W+", name.lower()) if len(p) > 2]
    return bool(parts) and parts[-1] in text  # at least the surname


def _prepare(c: Candidate, slot: BrollSlot, ctx: BrollContext) -> BrollChoice | None:
    """Download, find a cut-free window, contact sheet, podcast check."""
    if ctx.podcast_url and c.page_url and c.page_url.rstrip("/") == ctx.podcast_url.rstrip("/"):
        ctx.log(f"Rejected {c.key}: it is the podcast itself")
        return None
    try:
        path = media.fetch(c, ctx.cache, transport=ctx.transport)  # type: ignore[arg-type]
    except Exception as e:  # network/HTTP/yt-dlp errors: try the next candidate
        ctx.log(f"Couldn't download {c.key}: {e}")
        return None
    if ctx.podcast_sha1 and media.file_sha1(path) == ctx.podcast_sha1:
        ctx.log(f"Rejected {c.key}: same file as the podcast")
        return None
    need = slot.out_end - slot.out_start
    from clipforge.core.ingest import probe

    info = probe(path)
    if min(info.width or 0, info.height or 0) < 1080:
        ctx.log(f"Rejected {c.key}: below 1080p ({info.width}x{info.height})")
        return None
    start = media.cut_free_window(path, info.duration, need)
    if start is None:
        ctx.log(f"Rejected {c.key}: no {need:.1f} s stretch without a scene cut")
        return None
    sheet, frames = media.contact_sheet(path, start, need, ctx.sheets / f"{c.key}_{start:.2f}.jpg")
    if media.looks_like_podcast(frames, ctx.podcast_hashes):
        ctx.log(f"Rejected {c.key}: frames match the podcast footage")
        return None
    flags = []
    if slot.named_person and not name_in_metadata(slot.named_person, c):
        flags.append(f"unverified identity: '{slot.named_person}' isn't named in this footage")
    return BrollChoice(
        source=c.source,
        source_id=c.source_id,
        title=c.title,
        page_url=c.page_url,
        license=c.license,
        path=str(path),
        src_start=start,
        width=info.width or c.width,
        height=info.height or c.height,
        contact_sheet=str(sheet),
        flags=flags,
        podcast_checked=True,
    )


def _check(choice: BrollChoice, slot: BrollSlot, ctx: BrollContext) -> bool:
    if ctx.vision is None:
        return False
    ok, verdict = vision.check(
        ctx.vision,
        Path(choice.contact_sheet).read_bytes(),
        slot.beat_text,
        slot.named_person,
        choice.title,
        ctx.reject_rules,
        ctx.banned,
        ctx.log,
    )
    choice.verdict = verdict
    choice.rules_hash = ctx.rules
    if ok and not choice.flags:
        choice.approved_by = "vision"
        return True
    return False


def fill_slots(slots: list[BrollSlot], ctx: BrollContext) -> tuple[list[BrollSlot], QAReport]:
    qa = QAReport(step="broll")
    if not ctx.sources:
        qa.check(
            "B-roll sources configured",
            False,
            "no stock source (add PEXELS_API_KEY / PIXABAY_API_KEY), permitted YouTube "
            "footage or library footage",
            severity="warn",
        )
    out: list[BrollSlot] = []
    for slot in slots:
        if ctx.cancelled():
            raise InterruptedError("cancelled")
        decision = ctx.decisions.get(str(slot.index), {})
        prev = ctx.previous.get(slot.beat_text)
        slot = slot.model_copy(deep=True)
        if decision.get("action") == "clear":
            slot.status, slot.weak_reason = "empty", "cleared by the owner (stays on the speaker)"
            out.append(slot)
            continue
        if prev is not None:
            slot.candidates = prev.candidates
            slot.queries = slot.queries or prev.queries
            if prev.status == "filled" and prev.choice and not decision:
                slot.choice, slot.status = prev.choice, "filled"
                out.append(slot)
                continue
        if decision.get("action") == "manual" and decision.get("path"):
            from clipforge.broll.sources import Candidate

            manual = Candidate(
                source="manual",
                source_id=Path(decision["path"]).stem,
                title="owner's manual cover",
                page_url="",
                download_url=None,
                width=0,
                height=0,
                duration=0.0,
                license="owner's own footage",
                local_path=decision["path"],
            )
            choice = _prepare(manual, slot.model_copy(update={"named_person": None}), ctx)
            if choice:
                choice.approved_by = "owner"
                choice.rules_hash = ctx.rules
                slot.choice, slot.status, slot.weak_reason = choice, "filled", ""
            else:
                slot.status = "weak"
                slot.weak_reason = "WEAK: the uploaded cover failed the checks (see the log)"
            out.append(slot)
            continue
        if decision.get("action") == "approve":
            k = int(decision.get("candidate", -1))
            if 0 <= k < len(slot.candidates):
                choice = slot.candidates[k].model_copy(deep=True)
                if choice.verdict and not choice.verdict.get("accept", True):
                    choice.flags.append("owner approved it although the vision check said no")
                choice.approved_by = "owner"
                choice.rules_hash = ctx.rules
                slot.choice, slot.status, slot.weak_reason = choice, "filled", ""
                out.append(slot)
                continue
        if not slot.candidates:
            seen: set[str] = set()
            found: list[Candidate] = []
            for q in slot.queries:
                for src in ctx.sources:
                    try:
                        for c in src.search(q, per_page=6):
                            if c.key not in seen:
                                seen.add(c.key)
                                found.append(c)
                    except Exception as e:  # one source failing must not stop the others
                        ctx.log(f"{src.name} search failed for '{q}': {e}")
            for c in found:
                if len(slot.candidates) >= MAX_CANDIDATES:
                    break
                choice = _prepare(c, slot, ctx)
                if choice:
                    slot.candidates.append(choice)
        for choice in slot.candidates:
            if _check(choice, slot, ctx):
                slot.choice, slot.status, slot.weak_reason = choice, "filled", ""
                break
        if slot.status != "filled":
            slot.status = "weak"
            if not slot.candidates:
                slot.weak_reason = "WEAK: needs manual cover (no usable footage found)"
            elif ctx.vision is None:
                slot.weak_reason = "WEAK: needs manual cover (approve a candidate: no vision model)"
            else:
                slot.weak_reason = "WEAK: needs manual cover (no candidate passed the checks)"
        out.append(slot)
    filled = [s for s in out if s.status == "filled"]
    weak = [s for s in out if s.status == "weak"]
    if ctx.vision is not None and not getattr(ctx.vision, "real", True):
        qa.check("real vision check", False, "TEST DOUBLE vision model: not a real check")
    qa.check(
        "B-roll slots",
        True,
        f"{len(filled)} filled, {len(weak)} WEAK, {len(out) - len(filled) - len(weak)} cleared",
        severity="warn",
    )
    for s in weak:
        qa.check(
            f"slot {s.index + 1} at {s.out_start:.1f} s",
            False,
            f'{s.weak_reason} — beat: "{s.beat_text[:60]}"',
            severity="warn",
            at_seconds=s.out_start,
        )
    for s in filled:
        c = s.choice
        assert c is not None
        qa.check(
            f"slot {s.index + 1}: {c.source} footage checked",
            c.approved_by in ("vision", "owner") and c.rules_hash == ctx.rules,
            f"{c.title[:50]} ({c.width}x{c.height}), approved by {c.approved_by}"
            + (f"; {'; '.join(c.flags)}" if c.flags else ""),
            at_seconds=s.out_start,
        )
    qa.check(
        "no podcast footage in B-roll",
        all(s.choice is not None and s.choice.podcast_checked for s in filled),
        "every chosen shot passed the URL, file-hash and frame-hash checks against the podcast",
    )
    return out, qa


def reverify(slots: list[BrollSlot], ctx: BrollContext) -> QAReport:
    """Export gate: every chosen shot must have been checked against the current rules."""
    qa = QAReport(step="broll-export-check")
    for s in slots:
        if s.status != "filled" or not s.choice:
            continue
        c = s.choice
        if c.rules_hash == ctx.rules and c.approved_by:
            qa.check(f"slot {s.index + 1} checked against current rules", True, c.approved_by)
            continue
        if ctx.vision is not None and c.approved_by != "owner" and _check(c, s, ctx):
            qa.check(f"slot {s.index + 1} re-checked against current rules", True, "vision")
            continue
        qa.check(
            f"slot {s.index + 1} checked against current rules",
            False,
            "the banned-content list or reject rules changed after this shot was approved; "
            "approve it again or swap it",
            at_seconds=s.out_start,
        )
    return qa
