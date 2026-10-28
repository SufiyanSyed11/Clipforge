"""Phase 4: B-roll engine with synthetic footage, simulated stock APIs and test-double vision."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from clipforge.broll import beats, engine, media, queries, sources
from clipforge.editspec import BrollSlot, CaptionWord, EditSpec, HostInsert, Piece
from clipforge.llm.base import LLMError
from clipforge.llm.doubles import FakeLLM


def _video(path, src="testsrc2", seconds=4.0, size="1920x1080", second=None):
    """1080p synthetic footage; `second` adds a hard scene cut halfway."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if second:
        half = seconds / 2
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-t", str(half), "-f", "lavfi", "-i", f"{src}=size={size}:rate=30",
            "-t", str(half), "-f", "lavfi", "-i", f"{second}=size={size}:rate=30",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ]  # fmt: skip
    else:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-t", str(seconds), "-f", "lavfi", "-i", f"{src}=size={size}:rate=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return path


def _spec(duration=20.0, host=None) -> EditSpec:
    words = []
    t = 0.1
    k = 0
    while t + 0.3 < duration:
        text = "Hook ends here." if k == 3 else f"word{k}"
        words.append(
            CaptionWord(
                text=text, spoken=text, out_start=t, out_end=t + 0.3, src_start=t, src_end=t + 0.3
            )
        )
        t += 0.4
        k += 1
    return EditSpec(
        source_path="src.mp4",
        pieces=[Piece(src_start=0, src_end=duration)],
        captions=words,
        host_insert=host,
    )


# ---- beats ------------------------------------------------------------------------------------


def test_beats_every_2_to_3_seconds_62_percent_after_the_hook():
    slots = beats.plan_slots(_spec())
    assert slots and slots[0].out_start >= 1.2
    for a, b in zip(slots, slots[1:], strict=False):
        assert 2.0 - 0.05 <= b.out_start - a.out_start <= 3.0 + 0.05
    for s in slots:
        beat_len = (s.out_end - s.out_start) / beats.BROLL_SHARE
        assert 2.0 - 0.1 <= beat_len <= 3.0 + 0.1
        assert s.out_end <= 20.0 - beats.END_KEEP
        assert abs(s.out_start * 60 - round(s.out_start * 60)) < 1e-6


def test_beats_avoid_the_host_insert():
    host = HostInsert(
        out_start=5.0,
        out_end=7.0,
        src_start=0,
        src_end=2,
        cx=0,
        cy=0,
        crop_w=1,
        shot_id="s",
        reason="",
    )
    slots = beats.plan_slots(_spec(host=host))
    assert all(not (s.out_start < 7.0 and 5.0 < s.out_end) for s in slots)


# ---- queries ----------------------------------------------------------------------------------


def test_queries_from_local_model_or_labelled_keyword_fallback():
    slots = [
        BrollSlot(index=0, out_start=2, out_end=3.5, beat_text="I turned down two million dollars")
    ]
    out, method = queries.generate(slots, "context", [], FakeLLM(), print)
    assert method == "fake:test-double" and out[0].queries

    class Broken:
        name, model, real, context_tokens = "ollama", "qwen3:4b", True, 8192

        def complete_json(self, *a, **k):
            raise LLMError("Ollama isn't reachable")

    out, method = queries.generate(slots, "context", [], Broken(), print)
    assert method == "keyword fallback"
    assert any("million" in q or "dollars" in q for q in out[0].queries)


# ---- stock APIs (simulated) ----------------------------------------------------------------------


def test_pexels_keeps_only_1080p_files():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "KEY"
        return httpx.Response(
            200,
            json={
                "videos": [
                    {
                        "id": 1,
                        "url": "https://www.pexels.com/video/stadium-crowd-cheering-1/",
                        "duration": 12,
                        "video_files": [
                            {"width": 1280, "height": 720, "file_type": "video/mp4", "link": "a"},
                            {"width": 1920, "height": 1080, "file_type": "video/mp4", "link": "b"},
                            {"width": 3840, "height": 2160, "file_type": "video/mp4", "link": "c"},
                        ],
                    },
                    {
                        "id": 2,
                        "url": "https://www.pexels.com/video/low-res-2/",
                        "duration": 5,
                        "video_files": [
                            {"width": 960, "height": 540, "file_type": "video/mp4", "link": "d"}
                        ],
                    },
                ]
            },
        )

    src = sources.PexelsSource("KEY", transport=httpx.MockTransport(handler))
    found = src.search("stadium crowd")
    assert len(found) == 1
    c = found[0]
    assert (c.width, c.height, c.download_url) == (1920, 1080, "b")  # smallest 1080p+ file
    assert "stadium" in c.tags and "Pexels License" in c.license


def test_pixabay_parsing_and_1080p_filter():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["key"] == "K"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "id": 9,
                        "tags": "money, cash, dollars",
                        "pageURL": "https://pixabay.com/videos/id-9/",
                        "duration": 8,
                        "videos": {"large": {"url": "u", "width": 1920, "height": 1080}},
                    },
                    {
                        "id": 10,
                        "tags": "x",
                        "videos": {"medium": {"url": "v", "width": 1280, "height": 720}},
                    },
                ]
            },
        )

    found = sources.PixabaySource("K", transport=httpx.MockTransport(handler)).search("money")
    assert [c.source_id for c in found] == ["9"] and "dollars" in found[0].tags


# ---- the engine ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    lib = tmp_path_factory.mktemp("lib")
    _video(lib / "stadium_crowd.mp4", "testsrc2")
    (lib / "stadium_crowd.json").write_text(
        json.dumps({"title": "stadium crowd cheering", "tags": ["stadium", "crowd"]})
    )
    _video(lib / "money_cash.mp4", "smptebars", seconds=6, second="mandelbrot")  # scene cut at 3 s
    (lib / "money_cash.json").write_text(
        json.dumps({"title": "money cash dollars", "tags": ["money"]})
    )
    _video(lib / "watermark_city.mp4", "cellauto")
    (lib / "watermark_city.json").write_text(
        json.dumps({"title": "city skyline watermark", "tags": ["city"]})
    )
    _video(lib / "lowres_city.mp4", "testsrc", size="1280x720")
    return lib


def _ctx(tmp_path, lib, vision=True, podcast_hashes=None, **kw):
    return engine.BrollContext(
        sources=[sources.LibrarySource(lib)],
        vision=FakeLLM() if vision else None,
        reject_rules=["on-screen text", "watermarks"],
        banned=["women"],
        cache=tmp_path / "cache",
        sheets=tmp_path / "sheets",
        podcast_hashes=podcast_hashes or [],
        podcast_url=None,
        podcast_sha1=None,
        **kw,
    )


def _slot(i, queries, text="a line"):
    return BrollSlot(
        index=i, out_start=2.0 + i * 3, out_end=3.6 + i * 3, beat_text=text, queries=queries
    )


def test_fills_with_checked_1080p_footage_from_a_cut_free_window(tmp_path, library):
    out, qa = engine.fill_slots(
        [_slot(0, ["money cash"]), _slot(1, ["stadium crowd"])], _ctx(tmp_path, library)
    )
    money, stadium = out
    assert money.status == "filled" and stadium.status == "filled"
    # The money clip has a scene cut at 3.0 s: the chosen 1.6 s window must not contain it.
    s = money.choice.src_start
    assert not (s < 3.0 < s + 1.6)
    assert money.choice.width >= 1080 and money.choice.podcast_checked
    assert money.choice.approved_by == "vision" and money.choice.contact_sheet
    names = {c.name: c for c in qa.checks}
    assert names["no podcast footage in B-roll"].passed
    assert not names["real vision check"].passed  # test double is never a real check


def test_rejected_footage_and_nothing_good_means_weak(tmp_path, library):
    out, qa = engine.fill_slots([_slot(0, ["city skyline"])], _ctx(tmp_path, library))
    slot = out[0]
    # The only 1080p "city" clip has a watermark (vision says no); the 720p one is filtered.
    assert slot.status == "weak" and "no candidate passed" in slot.weak_reason
    assert all(c.width >= 1080 for c in slot.candidates)
    assert any("WEAK" in c.detail for c in qa.checks)


def test_no_sources_means_weak_needs_manual_cover(tmp_path, library):
    ctx = _ctx(tmp_path, library)
    ctx.sources = []
    out, qa = engine.fill_slots([_slot(0, ["stadium crowd"])], ctx)
    assert out[0].status == "weak" and "no usable footage" in out[0].weak_reason
    assert any(c.name == "B-roll sources configured" and not c.passed for c in qa.checks)


def test_podcast_footage_is_never_used(tmp_path, library):
    # Hash the stadium clip as if it were the podcast: it must be rejected.
    frames = media.sample_frames(library / "stadium_crowd.mp4", 0, 4, 2)
    ctx = _ctx(tmp_path, library, podcast_hashes=[media.dhash(f) for f in frames])
    out, _ = engine.fill_slots([_slot(0, ["stadium crowd"])], ctx)
    assert out[0].status == "weak"
    assert not any(c.source_id == "stadium_crowd" for c in out[0].candidates)


def test_without_vision_owner_approves_and_rules_changes_force_recheck(tmp_path, library):
    ctx = _ctx(tmp_path, library, vision=False)
    out, _ = engine.fill_slots([_slot(0, ["stadium crowd"])], ctx)
    slot = out[0]
    assert slot.status == "weak" and "approve a candidate" in slot.weak_reason and slot.candidates
    # Owner approves candidate 0 on the review screen.
    ctx2 = _ctx(
        tmp_path, library, vision=False, decisions={"0": {"action": "approve", "candidate": 0}}
    )
    ctx2.previous = {slot.beat_text: slot}
    out2, qa2 = engine.fill_slots([_slot(0, ["stadium crowd"])], ctx2)
    assert out2[0].status == "filled" and out2[0].choice.approved_by == "owner"
    assert engine.reverify(out2, ctx2).passed
    # The owner edits the banned-content list: the approval no longer covers the new rules.
    ctx3 = _ctx(tmp_path, library, vision=False)
    ctx3.banned = ["women", "alcohol"]
    rv = engine.reverify(out2, ctx3)
    assert not rv.passed and "changed after this shot was approved" in rv.failures[0].detail


def test_owner_can_clear_a_slot(tmp_path, library):
    ctx = _ctx(tmp_path, library, decisions={"0": {"action": "clear"}})
    out, _ = engine.fill_slots([_slot(0, ["stadium crowd"])], ctx)
    assert out[0].status == "empty" and "cleared" in out[0].weak_reason


def test_named_person_needs_their_name_in_the_footage(tmp_path, library):
    slot = _slot(0, ["stadium crowd"]).model_copy(update={"named_person": "Marcus Hale"})
    out, _ = engine.fill_slots([slot], _ctx(tmp_path, library))
    assert out[0].status == "weak"  # stand-in footage is never used silently
    assert any("unverified identity" in f for c in out[0].candidates for f in c.flags)
