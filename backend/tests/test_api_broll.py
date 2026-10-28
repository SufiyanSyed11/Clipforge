"""Phase 4 through the API: library footage fills slots, review screen actions, sources."""

from __future__ import annotations

import json

import pytest

from clipforge.jobs.worker import run_one
from clipforge.settings import get_settings
from tests.helpers import api_client
from tests.test_broll import _video


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setenv("FAKE_MODELS", "true")
    get_settings.cache_clear()


def _drain():
    while run_one("test") is not None:
        pass


def test_broll_fills_renders_and_review_actions(tmp_path, fake_models):
    from clipforge.testing.synth import write_fixture_video

    lib = get_settings().data_dir / "broll_library"
    # Library footage whose titles match the test-double search terms ("stadium crowd").
    for name, src in (("stadium_crowd_a", "testsrc2"), ("stadium_crowd_b", "smptebars")):
        _video(lib / f"{name}.mp4", src, seconds=5)
        (lib / f"{name}.json").write_text(
            json.dumps({"title": "stadium crowd", "tags": ["stadium"]})
        )

    c = api_client()
    cfg = c.get("/api/settings").json()
    cfg["style"]["width"], cfg["style"]["height"] = 360, 640
    c.put("/api/settings", json=cfg)
    video = write_fixture_video(tmp_path / "ep.mp4", seconds=60)
    with video.open("rb") as f:
        pid = c.post(
            "/api/projects",
            data={"rights_confirmed": "true", "clip_count": "1"},
            files={"file": ("ep.mp4", f, "video/mp4")},
        ).json()["id"]
    _drain()
    c.post(f"/api/projects/{pid}/host", json={"speaker": "S1"})
    _drain()
    clip = c.get(f"/api/projects/{pid}/clips").json()[0]
    d = c.get(f"/api/clips/{clip['id']}").json()
    broll = d["spec"]["broll"]
    filled = [s for s in broll if s["status"] == "filled"]
    assert filled, [s["weak_reason"] for s in broll]
    qa = {r["step"]: r for r in d["qa"]}
    assert "broll" in qa and qa["render-verify"]["passed"]
    assert any(
        ch["name"] == "real vision check" and not ch["passed"] for ch in qa["broll"]["checks"]
    )

    sheet = c.get(f"/api/clips/{clip['id']}/broll/{filled[0]['index']}/sheet")
    assert sheet.status_code == 200 and sheet.headers["content-type"] == "image/jpeg"

    # Owner clears a slot; on rebuild it stays on the speaker.
    slot = filled[0]["index"]
    r = c.patch(
        f"/api/clips/{clip['id']}/edits", json={"broll": [{"slot": slot, "action": "clear"}]}
    )
    assert r.status_code == 200
    bad = c.patch(
        f"/api/clips/{clip['id']}/edits",
        json={"broll": [{"slot": slot, "action": "approve", "candidate": 99}]},
    )
    assert bad.status_code == 422
    c.post(f"/api/clips/{clip['id']}/build")
    _drain()
    d2 = c.get(f"/api/clips/{clip['id']}").json()
    s2 = next(s for s in d2["spec"]["broll"] if s["index"] == slot)
    assert s2["status"] == "empty"


def test_permitted_sources_need_rights_and_never_the_podcast(tmp_path):
    from clipforge.db import Project, session_scope

    c = api_client()
    with session_scope() as s:
        p = Project(
            name="x", source_type="url", source="https://youtu.be/pod", rights_confirmed=True
        )
        s.add(p)
        s.flush()
        pid = p.id
    url = f"/api/projects/{pid}/broll-sources"
    assert (
        c.put(url, json=[{"url": "https://youtu.be/a", "rights_confirmed": False}]).status_code
        == 422
    )
    assert (
        c.put(url, json=[{"url": "https://youtu.be/pod", "rights_confirmed": True}]).status_code
        == 422
    )
    ok = c.put(
        url, json=[{"url": "https://youtu.be/a", "rights_confirmed": True, "license": "CC-BY"}]
    )
    assert ok.status_code == 200 and ok.json()["broll_sources"][0]["license"] == "CC-BY"
