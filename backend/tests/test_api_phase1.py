"""P1-1, P1-5, P1-8 (API side): rights gate, pipeline plumbing, one-click host confirmation.

Runs with FAKE_MODELS so no whisper model or API key is needed. These prove the plumbing,
not transcription or clip quality (those are the owner-PC golden runs).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clipforge.jobs.worker import run_one
from clipforge.settings import get_settings
from tests.helpers import api_client
from tests.media import make_video


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setenv("FAKE_MODELS", "true")
    get_settings.cache_clear()


def _drain():
    while run_one("test") is not None:
        pass


def _upload(c: TestClient, path, **form):
    with path.open("rb") as f:
        return c.post("/api/projects", data=form, files={"file": (path.name, f, "video/mp4")})


def test_project_without_rights_is_refused_and_nothing_is_queued(tmp_path):
    c = api_client()
    video = make_video(tmp_path / "ep.mp4", seconds=3)
    r = _upload(c, video, rights_confirmed="false")
    assert r.status_code == 422
    assert "rights" in r.json()["detail"]
    r = c.post("/api/projects", data={"url": "https://youtu.be/x", "rights_confirmed": "false"})
    assert r.status_code == 422
    assert c.get("/api/projects").json() == []
    assert c.get("/api/jobs").json() == []


def test_project_input_validation(tmp_path):
    c = api_client()
    assert (
        c.post("/api/projects", data={"rights_confirmed": "true"}).status_code == 422
    )  # neither url nor file
    assert (
        c.post("/api/projects", data={"rights_confirmed": "true", "url": "ftp://x"}).status_code
        == 422
    )
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    assert _upload(c, txt, rights_confirmed="true").status_code == 422


def _small_output(c: TestClient) -> None:
    """Render tests at 360x640 to keep them fast; the pipeline is identical at 1080x1920."""
    cfg = c.get("/api/settings").json()
    cfg["style"]["width"], cfg["style"]["height"] = 360, 640
    assert c.put("/api/settings", json=cfg).status_code == 200


def test_upload_to_ranked_clips_with_one_click_host(tmp_path, fake_models):
    from clipforge.testing.synth import write_fixture_video

    c = api_client()
    _small_output(c)
    video = write_fixture_video(tmp_path / "ep.mp4", seconds=75)
    r = _upload(c, video, rights_confirmed="true", name="Test episode", clip_count="3")
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    _drain()

    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "awaiting_host", p["jobs"][0]["error"]
    assert p["media_info"]["fps"] == 30
    assert p["host_suggestion"]["speaker"] == "S1"
    assert {s["speaker"] for s in p["speakers"]["stats"]} == {"S1", "S2"}
    job = next(j for j in p["jobs"] if j["kind"] == "process_project")
    steps = {q["step"]: q for q in job["qa"]}
    assert steps["ingest"]["passed"]
    # The test-double transcript is never reported as a real result.
    assert not steps["transcribe"]["passed"]
    assert c.get(f"/api/projects/{pid}/clips").json() == []

    assert c.post(f"/api/projects/{pid}/host", json={"speaker": "S9"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/host", json={"speaker": "S1"}).status_code == 200
    _drain()  # picks clips, then builds every clip
    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "clips_ready"
    clips = c.get(f"/api/projects/{pid}/clips").json()
    assert 1 <= len(clips) <= 3
    assert [cl["rank"] for cl in clips] == list(range(1, len(clips) + 1))
    assert all(cl["plan"]["duration"] <= 30 for cl in clips)

    # Every clip was cut in silence, rendered and put through the gate.
    for cl in clips:
        detail = c.get(f"/api/clips/{cl['id']}").json()
        assert detail["has_render"], detail["error"]
        spec = detail["spec"]
        assert spec["fps"] == 60 and (spec["width"], spec["height"]) == (360, 640)
        assert sum(pc["src_end"] - pc["src_start"] for pc in spec["pieces"]) <= 30
        qa = {r["step"]: r for r in detail["qa"]}
        assert qa["cut"]["passed"], qa["cut"]
        assert qa["render-verify"]["passed"], qa["render-verify"]
        # Gate is not passed on a test-double re-transcription: never a fake "verified".
        assert detail["status"] == "needs_review" and not detail["verified"]
        names = [ch["name"] for ch in qa["verify"]["checks"] if not ch["passed"]]
        assert names == ["re-transcription by a real model"], names
        v = c.get(f"/api/clips/{cl['id']}/video")
        assert v.status_code == 200 and v.headers["content-type"] == "video/mp4"
        wf = c.get(f"/api/clips/{cl['id']}/waveform").json()
        assert wf["db"] and wf["pieces"]

    first_text = clips[0]["plan"]["plan"]["segments"][0]["text"]
    # Changing the host is one click and re-picks with the new labels.
    assert c.post(f"/api/projects/{pid}/host", json={"speaker": "S2"}).status_code == 200
    _drain()
    p = c.get(f"/api/projects/{pid}").json()
    assert p["host_speaker"] == "S2" and p["status"] == "clips_ready"
    clips2 = c.get(f"/api/projects/{pid}/clips").json()
    assert clips2[0]["plan"]["plan"]["segments"][0]["text"] == first_text

    t = c.get(f"/api/projects/{pid}/transcript").json()
    assert t["fake"] is True and all(w["speaker"] for s in t["segments"] for w in s["words"])


def test_trim_edit_snaps_to_silence_and_rebuilds(tmp_path, fake_models):
    from clipforge.testing.synth import write_fixture_video

    c = api_client()
    _small_output(c)
    video = write_fixture_video(tmp_path / "ep.mp4", seconds=60)
    pid = _upload(c, video, rights_confirmed="true", clip_count="1").json()["id"]
    _drain()
    c.post(f"/api/projects/{pid}/host", json={"speaker": "S1"})
    _drain()
    clip = c.get(f"/api/projects/{pid}/clips").json()[0]
    before = c.get(f"/api/clips/{clip['id']}").json()["spec"]
    old_start = before["pieces"][0]["src_start"]
    # Drag the start handle ~3 s later, into the middle of speech.
    r = c.patch(
        f"/api/clips/{clip['id']}/edits",
        json={"trims": [{"segment": 0, "start": old_start + 3.0}]},
    )
    assert r.status_code == 200 and r.json()["edits"]["trims"]["0"][0] == old_start + 3.0
    assert c.post(f"/api/clips/{clip['id']}/build").status_code == 200
    assert c.post(f"/api/clips/{clip['id']}/build").status_code == 409  # already queued
    _drain()
    after = c.get(f"/api/clips/{clip['id']}").json()
    new_start = after["spec"]["pieces"][0]["src_start"]
    assert old_start < new_start
    start_cut = after["spec"]["cuts"][0]
    assert start_cut["reason"] == "trim"
    assert start_cut["silence_from"] <= new_start <= start_cut["silence_to"]
    assert {r["step"]: r for r in after["qa"]}["cut"]["passed"]


def test_cancel_a_queued_build(tmp_path, fake_models):
    from clipforge.testing.synth import write_fixture_video

    c = api_client()
    _small_output(c)
    video = write_fixture_video(tmp_path / "ep.mp4", seconds=60)
    pid = _upload(c, video, rights_confirmed="true", clip_count="1").json()["id"]
    _drain()
    c.post(f"/api/projects/{pid}/host", json={"speaker": "S1"})
    run_one("t")  # pick only; the build stays queued
    clip = c.get(f"/api/projects/{pid}/clips").json()[0]
    job = next(
        j
        for j in c.get("/api/jobs").json()
        if j["kind"] == "build_clip" and j["status"] == "queued"
    )
    assert c.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    _drain()
    assert c.get(f"/api/clips/{clip['id']}").json()["has_render"] is False
    retry = c.post(f"/api/jobs/{job['id']}/retry")
    assert retry.status_code == 201
    _drain()
    assert c.get(f"/api/clips/{clip['id']}").json()["has_render"] is True
    assert c.post(f"/api/jobs/{retry.json()['id']}/retry").status_code == 409  # it succeeded


def test_failed_ingest_marks_project_failed(tmp_path, fake_models):
    c = api_client()
    bad = make_video(tmp_path / "drift.mp4", seconds=6, audio_short_by=1.5)
    pid = _upload(c, bad, rights_confirmed="true").json()["id"]
    _drain()
    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "failed"
    assert "lengths match" in p["jobs"][0]["error"]
