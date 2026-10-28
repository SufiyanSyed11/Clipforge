"""P1-7 harness and the golden workflow's safety rails."""

from __future__ import annotations

import json

from clipforge.core.transcribe import fake_transcript
from clipforge.settings import get_settings
from clipforge.tools import golden, picker_harness


def test_harness_runs_versions_and_writes_comparison(tmp_path):
    t = fake_transcript(120)
    tp = tmp_path / "transcript.json"
    tp.write_text(t.model_dump_json())
    out = tmp_path / "out"
    assert (
        picker_harness.main(
            [str(tp), "--prompts", "v1", "v1", "--fake", "--out", str(out), "-n", "3"]
        )
        == 0
    )
    run = json.loads((out / "v1.json").read_text())
    assert run["fake"] is True and len(run["clips"]) <= 3
    md = (out / "compare.md").read_text()
    assert "FAKE stub" in md and "| 1 |" in md
    assert "Clips found by both versions" in md

    # Compare-only mode makes no model calls.
    out2 = tmp_path / "out2"
    rc = picker_harness.main(
        [str(tp), "--compare", str(out / "v1.json"), str(out / "v1.json"), "--out", str(out2)]
    )
    assert rc == 0 and (out2 / "compare.md").exists()


def test_golden_refuses_fake_models(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODELS", "true")
    get_settings.cache_clear()
    assert golden.main(["run"]) == 2


def test_golden_refuses_episodes_without_rights(monkeypatch, tmp_path):
    episodes = tmp_path / "episodes.json"
    monkeypatch.setattr(golden, "EPISODES", episodes)
    assert golden.main(["init"]) == 0
    data = json.loads(episodes.read_text())
    data["episodes"][0]["source"] = str(tmp_path / "ep.mp4")
    episodes.write_text(json.dumps(data))
    assert golden.main(["run"]) == 2  # rights_confirmed is false


def test_golden_init_never_overwrites(monkeypatch, tmp_path):
    episodes = tmp_path / "episodes.json"
    episodes.write_text('{"episodes": []}')
    monkeypatch.setattr(golden, "EPISODES", episodes)
    assert golden.main(["init"]) == 0
    assert episodes.read_text() == '{"episodes": []}'


def test_golden_quality_bar_reads_the_gate_checks():
    def rep(step, *checks):
        return {
            "step": step,
            "checks": [
                {"name": n, "passed": ok, "detail": d, "at_seconds": t} for n, ok, d, t in checks
            ],
        }

    qa = [
        rep(
            "verify",
            ("join at 4.200 s is in silence (pause)", True, "", 4.2),
            (
                "no word missing at a join or edge (zero clipped words)",
                False,
                "'went' at 9.10 s",
                9.1,
            ),
            ("no profanity audible in the cut", True, "none heard", None),
        ),
        rep("mix", ("voice at about -14 LUFS", True, "voice -14.2 LUFS", None)),
        rep("broll", ("slot 1: pexels footage checked", True, "", 2.0)),
    ]
    bar = {item: (res, detail) for item, res, detail in golden.quality_bar(qa)}
    assert bar["zero clipped words"][0] == "FAIL" and "9.10" in bar["zero clipped words"][1]
    assert bar["zero profanity leaks"][0] == "PASS"
    assert bar["voice at -14 LUFS"][0] == "PASS"
    assert bar["B-roll checked (no text/logos)"][0] == "PASS"
    assert bar["audio duration == video duration"][0] == "not measured"


def test_golden_episode_runs_the_full_pipeline_with_test_doubles(tmp_path, monkeypatch):
    """Plumbing only: FAKE_MODELS can't pass the gate, so no clip may come out verified."""
    from clipforge.settings import get_settings
    from clipforge.testing import synth

    monkeypatch.setenv("FAKE_MODELS", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(golden, "REPORT_DIR", tmp_path / "reports")
    video = synth.write_fixture_video(tmp_path / "ep.mp4", seconds=60)
    ep = {"id": "ep1", "title": "t", "source": str(video), "rights_confirmed": True}
    r = golden.run_episode(ep, n=1)
    assert r["built"], "no clip was built"
    clip = r["built"][0]
    assert clip["render"] and not clip["verified"]
    bar = dict((item, res) for item, res, _ in clip["quality_bar"])
    assert bar["zero clipped words"] == "FAIL"  # test-double transcriber is never "real"
    assert bar["audio duration == video duration"] == "PASS"
    md = golden.summary_markdown([r], None)
    assert "Quality bar" in md and "FAIL" in md
    assert (tmp_path / "reports" / "ep1" / "clip01-qa.json").exists()
