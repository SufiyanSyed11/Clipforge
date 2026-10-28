"""P1-1, P1-5, P1-8 (API side): rights gate, pipeline plumbing, one-click host confirmation.

Runs with FAKE_MODELS so no whisper model or API key is needed. These prove the plumbing,
not transcription or clip quality (those are the owner-PC golden runs).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.jobs.worker import run_one
from clipforge.settings import get_settings
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
    c = TestClient(create_app())
    video = make_video(tmp_path / "ep.mp4", seconds=3)
    r = _upload(c, video, rights_confirmed="false")
    assert r.status_code == 422
    assert "rights" in r.json()["detail"]
    r = c.post("/api/projects", data={"url": "https://youtu.be/x", "rights_confirmed": "false"})
    assert r.status_code == 422
    assert c.get("/api/projects").json() == []
    assert c.get("/api/jobs").json() == []


def test_project_input_validation(tmp_path):
    c = TestClient(create_app())
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


def test_upload_to_ranked_clips_with_one_click_host(tmp_path, fake_models):
    c = TestClient(create_app())
    video = make_video(tmp_path / "ep.mp4", seconds=75)
    r = _upload(c, video, rights_confirmed="true", name="Test episode", clip_count="3")
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    _drain()

    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "awaiting_host", p["jobs"][0]["error"]
    assert p["media_info"]["fps"] == 30
    assert p["host_suggestion"]["speaker"] == "S1"
    assert {s["speaker"] for s in p["speakers"]["stats"]} == {"S1", "S2"}
    job = p["jobs"][0]
    steps = {q["step"]: q for q in job["qa"]}
    assert steps["ingest"]["passed"]
    # The stub transcript is never reported as a real result.
    assert not steps["transcribe"]["passed"]
    assert c.get(f"/api/projects/{pid}/clips").json() == []

    assert c.post(f"/api/projects/{pid}/host", json={"speaker": "S9"}).status_code == 422
    assert c.post(f"/api/projects/{pid}/host", json={"speaker": "S1"}).status_code == 200
    _drain()
    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "clips_ready"
    clips = c.get(f"/api/projects/{pid}/clips").json()
    assert 1 <= len(clips) <= 3
    assert [cl["rank"] for cl in clips] == list(range(1, len(clips) + 1))
    assert all(cl["plan"]["duration"] <= 30 for cl in clips)
    first_text = clips[0]["plan"]["plan"]["segments"][0]["text"]

    # Changing the host is one click and re-picks with the new labels.
    assert c.post(f"/api/projects/{pid}/host", json={"speaker": "S2"}).status_code == 200
    _drain()
    p = c.get(f"/api/projects/{pid}").json()
    assert p["host_speaker"] == "S2" and p["status"] == "clips_ready"
    assert (
        c.get(f"/api/projects/{pid}/clips").json()[0]["plan"]["plan"]["segments"][0]["text"]
        == first_text
    )

    t = c.get(f"/api/projects/{pid}/transcript").json()
    assert t["fake"] is True and all(w["speaker"] for s in t["segments"] for w in s["words"])


def test_failed_ingest_marks_project_failed(tmp_path, fake_models):
    c = TestClient(create_app())
    bad = make_video(tmp_path / "drift.mp4", seconds=6, audio_short_by=1.5)
    pid = _upload(c, bad, rights_confirmed="true").json()["id"]
    _drain()
    p = c.get(f"/api/projects/{pid}").json()
    assert p["status"] == "failed"
    assert "lengths match" in p["jobs"][0]["error"]
