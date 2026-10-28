"""Phase 3 through the API: branding uploads, captions + mix + logo in the render."""

from __future__ import annotations

import io
import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.core.audio import write_wav
from clipforge.jobs.worker import run_one
from clipforge.settings import get_settings


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setenv("FAKE_MODELS", "true")
    get_settings.cache_clear()


def _drain():
    while run_one("test") is not None:
        pass


def test_branding_uploads_and_path_restriction(tmp_path):
    c = TestClient(create_app())
    png = cv2.imencode(".png", np.full((80, 200, 3), 255, np.uint8))[1].tobytes()
    r = c.post("/api/settings/logo", files={"file": ("logo.png", io.BytesIO(png), "image/png")})
    assert r.status_code == 200 and r.json()["logo_path"].endswith("logo.png")
    bad = c.post("/api/settings/logo", files={"file": ("x.exe", io.BytesIO(b"MZ"), "x")})
    assert bad.status_code == 422
    cfg = c.get("/api/settings").json()
    cfg["bgm_path"] = str(tmp_path / "elsewhere.mp3")  # not uploaded through Settings
    assert c.put("/api/settings", json=cfg).status_code == 422
    assert c.delete("/api/settings/logo").json()["logo_path"] is None


def test_build_with_logo_music_and_captions(tmp_path, fake_models):
    from clipforge.testing.synth import write_fixture_video

    c = TestClient(create_app())
    cfg = c.get("/api/settings").json()
    cfg["style"]["width"], cfg["style"]["height"] = 360, 640
    assert c.put("/api/settings", json=cfg).status_code == 200
    png = cv2.imencode(".png", np.full((60, 160, 3), (0, 140, 255), np.uint8))[1].tobytes()
    c.post("/api/settings/logo", files={"file": ("logo.png", io.BytesIO(png), "image/png")})
    t = np.arange(48000 * 8) / 48000
    write_wav(tmp_path / "bed.wav", (0.3 * np.sin(2 * np.pi * 196 * t)).astype(np.float32))
    with (tmp_path / "bed.wav").open("rb") as f:
        assert (
            c.post("/api/settings/music", files={"file": ("bed.wav", f, "audio/wav")}).status_code
            == 200
        )

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
    qa = {r["step"]: r for r in d["qa"]}
    assert qa["mix"]["passed"], qa["mix"]
    names = {ch["name"] for ch in qa["mix"]["checks"]}
    assert "music about 19 dB under the voice (ducked)" in names
    assert qa["captions"]["passed"] and qa["render-verify"]["passed"]
    assert "framing" in qa  # no faces in a test pattern: centre crop with a warning
    spec = d["spec"]
    assert spec["logo_path"] and spec["captions"]
    # Captions file and SRT exist next to the render; the logo is in the top-right corner.
    from clipforge.storage import get_storage

    cdir = get_storage().project_dir(pid) / "clips" / f"{clip['id']:04d}"
    assert (cdir / "captions.ass").exists() and (cdir / "captions.srt").exists()
    cap = cv2.VideoCapture(str(cdir / "render.mp4"))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 30)
    ok, frame = cap.read()
    assert ok
    h, w = frame.shape[:2]
    corner = frame[
        int(h * 0.06) : int(h * 0.06) + 10, w - int(w * 0.05) - 40 : w - int(w * 0.05) - 10
    ]
    # orange logo (BGR 0,140,255) after the colour grade: red channel high, blue low
    assert corner[..., 2].mean() > 150 and corner[..., 0].mean() < 100
    json.dumps(d)  # serialisable
