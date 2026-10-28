"""Phase 5: copy pack rules, export gate and pack, through the API."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.core import copywriter, export
from clipforge.jobs.worker import run_one
from clipforge.qa import QAReport
from clipforge.settings import get_settings

GOOD = {
    "title": 'He Said No To $2M: "You Can\'t Buy This Moment" 😮',
    "youtube_description": "Marcus Hale turned down two million 💰\nWould you? Comment 👇",
    "instagram_caption": "He said no in five minutes. #money",
    "instagram_tags": ["#nba", "money", "#Motivation", "#nba", "#sports", "#extra1", "#extra2"],
    "x_post": "Marcus Hale turned down two million dollars. His coach told him why.",
}


def test_normalize_enforces_the_spec_rules():
    pack = copywriter.normalize(dict(GOOD), "#DoubleCoverage", "")
    assert pack.instagram_tags[0] == "#DoubleCoverage"
    assert len(pack.instagram_tags) == 5  # brand + related, max 5 in total
    assert len({t.lower() for t in pack.instagram_tags}) == 5  # de-duplicated
    assert "#money" not in pack.instagram_caption  # hashtags live in the tag list
    assert copywriter.copy_qa(pack, "#DoubleCoverage").passed


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("title", "No emoji here", "exactly one emoji"),
        ("title", "Two 😮 emojis 🔥", "exactly one emoji"),
        ("x_post", "So good 🔥.", "no emojis|must not contain emojis"),
        ("x_post", "One. Two. Three.", "1-2 sentences"),
        ("youtube_description", "No emoji and no question.", "emojis"),
        ("youtube_description", "Has an emoji 🔥 and that is all.", "call to comment"),
    ],
)
def test_normalize_rejects_rule_breaks(field, value, match):
    with pytest.raises(copywriter.CopyInvalid, match=match):
        copywriter.normalize({**GOOD, field: value}, "#DoubleCoverage", "")


def test_credit_line_only_when_set():
    raw = {**GOOD, "instagram_caption": "Great clip.\nCredit: @someone"}
    assert "Credit" not in copywriter.normalize(raw, "#DC", "").instagram_caption
    pack = copywriter.normalize(dict(GOOD), "#DC", "Clip from The Show (used with permission)")
    assert pack.youtube_description.endswith("(used with permission)")


class Scripted:
    name, model, real, context_tokens = "scripted", "x", True, None

    def __init__(self, *answers):
        self.answers = list(answers)
        self.users = []

    def complete_json(self, system, user, schema, log, **kw):
        assert schema is copywriter.SCHEMA
        self.users.append(user)
        return json.dumps(self.answers.pop(0))


def test_write_copy_retries_with_feedback_then_succeeds():
    p = Scripted({**GOOD, "title": "No emoji"}, GOOD)
    pack, qa = copywriter.write_copy(p, "text", "hook", "payoff", ["t"], "#DC", "", print)
    assert pack.title == GOOD["title"] and qa.passed
    assert "exactly one emoji" in p.users[1]  # the rule break was fed back
    assert any(c.name == "copy needed retries" for c in qa.checks)


def test_write_copy_fails_safely():
    p = Scripted(*([{**GOOD, "x_post": "🔥🔥"}] * 3))
    with pytest.raises(copywriter.CopyInvalid, match="every time"):
        copywriter.write_copy(p, "t", "h", "p", [], "#DC", "", print)


def test_export_gate_reasons():
    ok = QAReport(step="broll-export-check")
    pack = copywriter.normalize(dict(GOOD), "#DC", "")
    assert export.export_gate(True, [], ok, pack, True) == []
    qa = [
        {
            "step": "verify",
            "checks": [{"name": "no word missing", "passed": False, "at_seconds": 3.2}],
        }
    ]
    reasons = export.export_gate(False, qa, ok, None, True)
    assert any("no word missing @ 3.20 s" in r for r in reasons)
    assert "no copy pack yet" in reasons
    bad = QAReport(step="broll-export-check")
    bad.check("slot 1 checked against current rules", False, "rules changed")
    assert any("B-roll" in r for r in export.export_gate(True, [], bad, pack, True))


# ---- API flow ---------------------------------------------------------------------------------


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setenv("FAKE_MODELS", "true")
    get_settings.cache_clear()


def _drain():
    while run_one("test") is not None:
        pass


def test_copy_edit_and_export_through_the_api(tmp_path, fake_models):
    from clipforge.testing.synth import write_fixture_video

    c = TestClient(create_app())
    cfg = c.get("/api/settings").json()
    cfg["style"]["width"], cfg["style"]["height"] = 360, 640
    c.put("/api/settings", json=cfg)
    video = write_fixture_video(tmp_path / "ep.mp4", seconds=60)
    with video.open("rb") as f:
        pid = c.post(
            "/api/projects",
            data={"rights_confirmed": "true", "clip_count": "1", "name": "Episode 12"},
            files={"file": ("ep.mp4", f, "video/mp4")},
        ).json()["id"]
    _drain()
    c.post(f"/api/projects/{pid}/host", json={"speaker": "S1"})
    _drain()  # pick, build, then write the copy automatically
    clip = c.get(f"/api/projects/{pid}/clips").json()[0]
    pack = clip["copy_pack"]
    assert pack and pack["instagram_tags"][0] == "#DoubleCoverage"
    assert len(pack["instagram_tags"]) <= 5

    # Owner edit that breaks a rule is refused; a valid edit is saved and marked edited.
    bad = c.put(f"/api/clips/{clip['id']}/copy", json={**GOOD, "title": "no emoji"})
    assert bad.status_code == 422 and "emoji" in bad.json()["detail"]
    ok = c.put(f"/api/clips/{clip['id']}/copy", json=GOOD)
    assert ok.status_code == 200 and ok.json()["copy_pack"]["edited"] is True

    # Export: test doubles can't pass the gate, so the pack is marked UNVERIFIED-TEST.
    assert c.post(f"/api/clips/{clip['id']}/export").status_code == 200
    _drain()
    z = c.get(f"/api/clips/{clip['id']}/export")
    assert z.status_code == 200
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        names = {n.split("/", 1)[1] for n in zf.namelist()}
        assert {
            "video.mp4", "transcript.txt", "transcript.json", "shot_list.csv", "captions.srt",
            "captions.ass", "copy.md", "copy.json", "qa.json", "manifest.json",
        } <= names  # fmt: skip
        folder = zf.namelist()[0].split("/")[0]
        assert folder.endswith("-UNVERIFIED-TEST")
        manifest = json.loads(zf.read(f"{folder}/manifest.json"))
        assert manifest["verified"] is False and manifest["unverified_test_export"] is True
        assert "video.mp4" in manifest["files"]
        assert "speaker" in zf.read(f"{folder}/shot_list.csv").decode()


def test_export_is_blocked_outside_test_mode(tmp_path, fake_models, monkeypatch):
    from clipforge.testing.synth import write_fixture_video

    c = TestClient(create_app())
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
    # Same unverified clip, but as a real run: the gate must block it.
    monkeypatch.setenv("FAKE_MODELS", "false")
    get_settings.cache_clear()
    job = c.post(f"/api/clips/{clip['id']}/export").json()
    run_one("t")
    j = c.get(f"/api/jobs/{job['id']}").json()
    assert j["status"] == "failed" and "Export blocked" in j["error"]
    assert "verification gate failed" in j["error"]
    assert c.get(f"/api/clips/{clip['id']}/export").status_code == 404
