"""P1-1, P1-2: rights gate and ingest."""

from __future__ import annotations

import sys

import pytest

from clipforge.core import ingest
from tests.media import make_audio, make_video


def test_rights_gate_blocks_ingest():
    with pytest.raises(ingest.RightsNotConfirmed):
        ingest.require_rights(False)
    ingest.require_rights(True)


def test_ytdlp_is_one_call_producing_one_muxed_file(tmp_path):
    cmd = ingest.ytdlp_command("https://www.youtube.com/watch?v=abc", tmp_path)
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"]
    fmt = cmd[cmd.index("-f") + 1]
    # Video + audio merged by yt-dlp in this one call, or a pre-muxed file.
    assert fmt == "bv*[height<=1080]+ba/b[height<=1080]/b"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert "--no-playlist" in cmd
    # Never partial downloads (sections drift when joined).
    assert not any(a.startswith("--download-sections") for a in cmd)
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc"


@pytest.mark.parametrize("bad", ["", "ftp://x.com/a", "not a url", "javascript:alert(1)"])
def test_bad_urls_rejected(bad):
    with pytest.raises(ingest.IngestError):
        ingest.validate_url(bad)


def test_probe_and_qa_on_muxed_video(tmp_path):
    info = ingest.probe(make_video(tmp_path / "ep.mp4", seconds=6, fps=30))
    assert info.has_video and info.has_audio
    assert info.fps == 30
    assert (info.width, info.height) == (320, 240)
    assert abs(info.duration - 6) < 0.2
    qa = ingest.ingest_qa(info)
    assert qa.passed, qa.failures


def test_drifted_av_lengths_fail_qa(tmp_path):
    info = ingest.probe(make_video(tmp_path / "bad.mp4", seconds=6, audio_short_by=1.0))
    qa = ingest.ingest_qa(info)
    assert not qa.passed
    assert any("lengths match" in c.name for c in qa.failures)


def test_audio_only_file_passes_with_warning(tmp_path):
    info = ingest.probe(make_audio(tmp_path / "ep.wav", seconds=4))
    qa = ingest.ingest_qa(info)
    assert qa.passed
    assert not info.has_video


def test_extract_audio_16k_mono(tmp_path):
    src = make_video(tmp_path / "ep.mp4", seconds=3)
    wav = ingest.extract_audio(src, tmp_path / "a.wav")
    audio, sr = ingest.load_wav(wav)
    assert sr == 16000
    assert abs(len(audio) / sr - 3) < 0.1
    assert not (tmp_path / "a.tmp.wav").exists()


def test_probe_unreadable_file(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(ingest.IngestError):
        ingest.probe(bad)
