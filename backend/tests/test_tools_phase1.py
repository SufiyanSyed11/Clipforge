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
