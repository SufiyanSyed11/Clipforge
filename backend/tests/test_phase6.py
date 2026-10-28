"""Phase 6: learning loop, YouTube client (mock transport), posting helpers, disk management."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from clipforge.core import disk, learn, posting, youtube
from clipforge.db import Clip, Job, Project, session_scope
from clipforge.settings import get_settings
from tests.helpers import api_client

NOW = datetime.now(UTC)


def _stat(title, views, hours_ago, dur=24.0, pct=60.0):
    return learn.VideoStat(
        title=title,
        views=views,
        published_at=NOW - timedelta(hours=hours_ago),
        duration_s=dur,
        avg_view_pct=pct,
    )


# ---- learning ---------------------------------------------------------------------------------


def test_analyse_finds_what_the_top_half_shares():
    vids = [
        _stat('Jalen Brunt says "nobody guards me like that" 🏀', 90000, 20, dur=22),
        _stat('Mike Torres: "he cried in the tunnel" 😳', 80000, 30, dur=23),
        _stat("thoughts on the trade deadline and other things we discussed today", 900, 25, 40),
        _stat("our full reaction to the draft lottery results this year", 700, 28, 41),
    ]
    ins = learn.analyse(vids, "test", 3)
    assert ins.videos == 4
    assert ins.best_length == "20-25 s"
    assert ins.title_patterns["one emoji"] > 0
    assert ins.title_patterns["quoted punchline"] > 0
    assert ins.top_titles[0].startswith("Jalen")
    assert "Best length: 20-25 s" in ins.summary
    assert ins.best_hours  # real publish times


def test_date_only_exports_do_not_invent_an_upload_hour():
    vids = learn.parse_csv(STUDIO_CSV)
    assert learn.analyse(vids, "csv", 3).best_hours == []


def test_analyse_with_too_little_data_says_so():
    ins = learn.analyse([_stat("one", 10, 5)], "test", 3)
    assert ins.videos == 1 and "not enough" in ins.summary
    assert learn.why_this_clip({"title_options": ["x"]}, 22, ins) == []


def test_why_this_clip_and_prompt_context_freshness():
    vids = [
        _stat('Jalen Brunt says "nobody guards me like that" 🏀', 90000, 20, dur=22),
        _stat("long boring title about the trade deadline and other things", 900, 25, dur=40),
    ]
    ins = learn.analyse(vids, "test", 3)
    notes = learn.why_this_clip(
        {"title_options": ['Ann Lee: "it was rigged from day one" 🔥'], "hook": "", "payoff": ""},
        22.0,
        ins,
    )
    assert any("20-25 s" in n for n in notes)
    assert any("one emoji" in n for n in notes)
    learn.save_insights(ins)
    assert learn.learning_context().startswith("Channel performance")
    stale = ins.model_copy(update={"updated_at": NOW - timedelta(days=30)})
    learn.save_insights(stale)
    assert learn.learning_context() == ""


# fmt: off
STUDIO_CSV = "\ufeff" + """Content,Video title,Video publish time,Duration,Views,\
Average view duration,Average percentage viewed (%)
Total,,,,12345,,
abc,"Quote ""with comma, inside"" 🏀",2026-10-27,0:24,9000,0:18,75.5
def,Plain title,"Oct 26, 2026",31,"1,000",0:05,20
"""
# fmt: on


def test_parse_studio_csv():
    vids = learn.parse_csv(STUDIO_CSV)
    assert len(vids) == 2  # the Total row is skipped
    a = vids[0]
    assert a.title.startswith("Quote") and a.views == 9000
    assert a.duration_s == 24 and a.avg_view_s == 18 and a.avg_view_pct == 75.5
    assert a.published_at == datetime(2026, 10, 27, tzinfo=UTC)
    assert vids[1].published_at == datetime(2026, 10, 26, tzinfo=UTC)
    assert vids[1].views == 1000 and vids[1].avg_view_pct == 20


def test_parse_csv_rejects_unknown_layout():
    with pytest.raises(ValueError, match="title and a views column"):
        learn.parse_csv("a,b\n1,2\n")


def test_recent_filters_by_date_but_keeps_undated_exports():
    old = _stat("old", 1, 24 * 10)
    new = _stat("new", 1, 5)
    assert [v.title for v in learn.recent([old, new], 3)] == ["new"]
    undated = learn.VideoStat(title="x", views=1)
    assert learn.recent([undated], 3) == [undated]


# ---- YouTube client against a mock transport --------------------------------------------------


def test_oauth_uses_pkce_and_state():
    o = youtube.OAuth("cid", "secret", "http://127.0.0.1:8000/cb")
    url, pending = o.start(allow_upload=False)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == [pending["state"]]
    assert youtube.UPLOAD_SCOPE not in q["scope"][0]
    _url2, p2 = o.start(allow_upload=True)
    assert p2["state"] != pending["state"]


def test_token_exchange_refresh_and_file_permissions(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = parse_qs(req.content.decode())
        calls.append(body)
        if body["grant_type"] == ["authorization_code"]:
            assert body["code_verifier"] == ["v"]
            return httpx.Response(
                200, json={"access_token": "a1", "refresh_token": "r", "expires_in": 3600}
            )
        return httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})

    o = youtube.OAuth("cid", "secret", "http://x/cb", transport=httpx.MockTransport(handler))
    tok = o.finish("code", "v")
    youtube.save_token(tmp_path, tok)
    path = tmp_path / "secrets" / "youtube_token.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    tok["expires_at"] = time.time() - 1
    youtube.save_token(tmp_path, tok)

    def api(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer a2"  # refreshed before the call
        return httpx.Response(200, json={"items": []})

    yt = youtube.YouTube(o, tmp_path, transport=httpx.MockTransport(api))
    with pytest.raises(youtube.YouTubeError, match="No channel"):
        yt.recent_uploads(3)
    assert json.loads(path.read_text())["access_token"] == "a2"
    assert calls[-1]["grant_type"] == ["refresh_token"]


def test_refused_sign_in_raises():
    o = youtube.OAuth(
        "c", "s", "http://x/cb", transport=httpx.MockTransport(lambda r: httpx.Response(400))
    )
    with pytest.raises(youtube.YouTubeError, match="refused"):
        o.finish("code", "v")


def _yt_api(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    recent = (NOW - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if path.endswith("/channels"):
        return httpx.Response(
            200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        )
    if path.endswith("/playlistItems"):
        return httpx.Response(
            200,
            json={
                "items": [
                    {"contentDetails": {"videoId": "v1", "videoPublishedAt": recent}},
                    {"contentDetails": {"videoId": "v2", "videoPublishedAt": recent}},
                    {"contentDetails": {"videoId": "old", "videoPublishedAt": old}},
                ]
            },
        )
    if path.endswith("/videos"):
        assert req.url.params["id"] == "v1,v2"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": vid,
                        "snippet": {"title": f"t {vid}", "publishedAt": recent},
                        "contentDetails": {"duration": "PT24S"},
                        "statistics": {"viewCount": str(n)},
                    }
                    for vid, n in (("v1", 1000), ("v2", 50))
                ]
            },
        )
    if path.endswith("/reports"):
        return httpx.Response(200, json={"rows": [["v1", 1000, 20.0, 83.0]]})
    return httpx.Response(404)


def test_recent_uploads_reads_stats_and_retention(tmp_path):
    youtube.save_token(tmp_path, {"access_token": "a", "expires_at": time.time() + 999})
    o = youtube.OAuth("c", "s", "http://x/cb")
    yt = youtube.YouTube(o, tmp_path, transport=httpx.MockTransport(_yt_api))
    stats = {v.video_id: v for v in yt.recent_uploads(3)}
    assert set(stats) == {"v1", "v2"}
    assert stats["v1"].duration_s == 24 and stats["v1"].avg_view_pct == 83.0
    assert stats["v2"].avg_view_pct is None


def test_upload_is_private_and_scheduled(tmp_path):
    youtube.save_token(tmp_path, {"access_token": "a", "expires_at": time.time() + 999})
    video = tmp_path / "c.mp4"
    video.write_bytes(b"\0" * 100)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            seen["meta"] = json.loads(req.content)
            return httpx.Response(200, headers={"location": "https://upload.example/session"})
        seen["bytes"] = len(req.content)
        return httpx.Response(200, json={"id": "NEWID"})

    yt = youtube.YouTube(
        youtube.OAuth("c", "s", "x"), tmp_path, transport=httpx.MockTransport(handler)
    )
    when = NOW + timedelta(days=1)
    vid = yt.upload_private_scheduled(video, "T", "D", ["#DoubleCoverage", "#nba"], when)
    assert vid == "NEWID" and seen["bytes"] == 100
    assert seen["meta"]["status"]["privacyStatus"] == "private"
    assert seen["meta"]["status"]["publishAt"].endswith("Z")
    assert seen["meta"]["snippet"]["tags"] == ["DoubleCoverage", "nba"]


# ---- posting ----------------------------------------------------------------------------------


def test_suggest_time_uses_best_hour_and_is_in_the_future():
    ins = learn.Insights(updated_at=NOW, source="t", window_days=3, videos=4, best_hours=[17])
    now = datetime(2026, 10, 28, 18, 30, tzinfo=UTC)
    when, why = posting.suggest_time(ins, now)
    assert when == datetime(2026, 10, 29, 17, 0, tzinfo=UTC) and "17:00 UTC" in why
    when2, why2 = posting.suggest_time(None, now)
    assert when2 >= now + timedelta(hours=1) and "default" in why2


def test_ics_escapes_text():
    text = posting.ics("Title; with, chars", NOW, "line1\nline2", "clip-1")
    assert "SUMMARY:Post: Title\\; with\\, chars" in text
    assert "line1\\nline2" in text
    assert text.startswith("BEGIN:VCALENDAR") and "\r\n" in text


# ---- learning API -----------------------------------------------------------------------------


def test_learning_api_csv_import_and_guards(monkeypatch):
    c = api_client()
    r = c.get("/api/learning").json()
    assert r["insights"] is None and r["youtube"]["connected"] is False
    assert c.post("/api/learning/youtube/connect", json={}).status_code == 422  # no client id
    assert c.post("/api/learning/refresh", json={"days": 3}).status_code == 422  # not connected
    bad = c.get("/api/learning/youtube/callback", params={"code": "x", "state": "unknown"})
    assert bad.status_code == 400
    csv_text = STUDIO_CSV.replace("2026-10-27", NOW.strftime("%Y-%m-%d")).replace(
        '"Oct 26, 2026"', NOW.strftime("%Y-%m-%d")
    )
    r = c.post(
        "/api/learning/csv?days=3",
        files={"file": ("studio.csv", csv_text.encode(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["insights"]["videos"] == 2
    assert r.json()["prompt_context"].startswith("Channel performance")
    r = c.post("/api/learning/csv", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422


def test_connect_returns_google_url(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "sec")
    get_settings.cache_clear()
    r = api_client().post("/api/learning/youtube/connect", json={"allow_upload": True})
    assert r.status_code == 200
    url = r.json()["auth_url"]
    assert url.startswith(youtube.AUTH_URL) and "youtube.upload" in url


def _clip(verified=True, copy=True) -> int:
    with session_scope() as s:
        p = Project(name="p", source_type="upload", source="x", rights_confirmed=True)
        s.add(p)
        s.flush()
        clip = Clip(project_id=p.id, rank=1, plan={}, verified=verified)
        if copy:
            clip.copy_pack = {
                "title": "T; x",
                "youtube_description": "d",
                "instagram_caption": "c",
                "instagram_tags": ["#DoubleCoverage"],
                "x_post": "x post",
            }
        s.add(clip)
        s.flush()
        return clip.id


def test_youtube_schedule_validation():
    cid = _clip()
    c = api_client()
    soon = (NOW + timedelta(minutes=5)).isoformat()
    later = (NOW + timedelta(days=1)).isoformat()
    assert c.post(f"/api/clips/{cid}/youtube", json={"publish_at": "nope"}).status_code == 422
    assert c.post(f"/api/clips/{cid}/youtube", json={"publish_at": soon}).status_code == 422
    naive = (NOW + timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert c.post(f"/api/clips/{cid}/youtube", json={"publish_at": naive}).status_code == 422
    r = c.post(f"/api/clips/{cid}/youtube", json={"publish_at": later})
    assert r.status_code == 422 and "upload permission" in r.json()["detail"]


def test_calendar_download():
    cid = _clip()
    r = api_client().get(f"/api/clips/{cid}/post.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:Post: T\\; x" in r.text
    assert api_client().get(f"/api/clips/{_clip(copy=False)}/post.ics").status_code == 404
    plan = api_client().get(f"/api/clips/{cid}/posting").json()
    assert datetime.fromisoformat(plan["suggested_at"]) > NOW and plan["posts"] == []
    assert plan["youtube"]["can_upload"] is False


# ---- disk -------------------------------------------------------------------------------------


def _fake_project(data, pid=1):
    pdir = data / "projects" / f"{pid:05d}"
    (pdir / "source").mkdir(parents=True)
    (pdir / "source" / "source.mp4").write_bytes(b"s" * 2000)
    (pdir / "audio48k.wav").write_bytes(b"a" * 5000)
    (pdir / "audio16k.wav").write_bytes(b"b" * 1000)
    clip = pdir / "clips" / "0001"
    (clip / "mix").mkdir(parents=True)
    (clip / "mix" / "bgm.wav").write_bytes(b"m" * 700)
    (clip / "voice.wav").write_bytes(b"v" * 300)
    (clip / "mix.wav").write_bytes(b"x" * 300)
    (clip / "render.mp4").write_bytes(b"r" * 400)
    (clip / "spec.json").write_text("{}")
    return pdir


def test_intermediates_cleanup_keeps_sources_and_renders(tmp_path):
    pdir = _fake_project(tmp_path)
    names = {p.name for p in disk.intermediates(pdir)}
    assert names == {"audio48k.wav", "audio16k.wav", "voice.wav", "mix.wav", "mix"}
    freed = disk.clean_intermediates(pdir)
    assert freed == 5000 + 1000 + 700 + 300 + 300
    assert (pdir / "source" / "source.mp4").exists()
    assert (pdir / "clips" / "0001" / "render.mp4").exists()
    assert (pdir / "clips" / "0001" / "spec.json").exists()
    assert disk.intermediates(pdir) == []


def test_require_free_fails_loudly(tmp_path):
    disk.require_free(tmp_path, 0.0)
    with pytest.raises(disk.LowDiskSpace, match="MIN_FREE_GB"):
        disk.require_free(tmp_path, 10**6, what="the download")


def test_storage_api_usage_and_cleanup():
    data = get_settings().data_dir
    c = api_client()
    pid = _clip()
    with session_scope() as s:
        pid = s.get(Clip, pid).project_id
        s.add(Job(kind="ping", project_id=pid, status="done"))
    _fake_project(data, pid)
    (data / "broll_cache").mkdir(parents=True, exist_ok=True)
    (data / "broll_cache" / "a.mp4").write_bytes(b"c" * 100)
    u = c.get("/api/storage").json()
    assert u["per_project"][0]["project_id"] == pid and u["broll_cache_gb"] >= 0
    assert "low" in u
    r = c.post("/api/storage/cleanup", json={"target": "intermediates", "project_id": pid})
    assert r.status_code == 200 and not (data / "projects" / f"{pid:05d}" / "audio48k.wav").exists()
    c.post("/api/storage/cleanup", json={"target": "broll_cache"})
    assert list((data / "broll_cache").iterdir()) == []
    # Deleting a project needs explicit confirmation.
    r = c.post("/api/storage/cleanup", json={"target": "project", "project_id": pid})
    assert r.status_code == 422
    r = c.post(
        "/api/storage/cleanup", json={"target": "project", "project_id": pid, "confirm": True}
    )
    assert r.status_code == 200, r.text
    assert not (data / "projects" / f"{pid:05d}").exists()
    with session_scope() as s:
        assert s.get(Project, pid) is None
        assert s.query(Job).filter(Job.kind == "ping").one().project_id is None
    assert c.post("/api/storage/cleanup", json={"target": "bogus"}).status_code == 422


# ---- GPU memory handoff (RTX 3050 6 GB) -------------------------------------------------------


def test_gpu_gate_transcription_unloads_text_and_vision_models(monkeypatch):
    from clipforge.core import transcribe
    from clipforge.pipeline import clip_tasks, tasks

    unloaded = []

    class Prov:
        def __init__(self, name):
            self.name = name

        def unload(self):
            unloaded.append(self.name)

    monkeypatch.setattr(tasks, "get_text_provider", lambda: Prov("qwen3:4b"))
    monkeypatch.setattr(tasks, "get_vision_provider", lambda: Prov("qwen2.5vl:3b"))
    monkeypatch.setattr(clip_tasks.verify, "whisper_transcriber", lambda cfg: "whisper")
    logs = []

    class Ctx:
        def log(self, m, **_):
            logs.append(m)

    for device, expect in (("cpu", []), ("cuda", ["qwen3:4b", "qwen2.5vl:3b"])):
        unloaded.clear()
        real_cfg = dataclasses.replace(
            transcribe.resolve_config(get_settings(), None), device=device
        )
        monkeypatch.setattr(transcribe, "resolve_config", lambda s, r, c=real_cfg: c)
        assert clip_tasks.make_transcriber(Ctx()) == ("whisper", True)
        assert unloaded == expect
    assert any("free GPU memory" in m for m in logs)
