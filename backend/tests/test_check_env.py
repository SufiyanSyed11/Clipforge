"""P0-5: environment report and hardware-derived defaults."""

from __future__ import annotations

import json

from clipforge.tools import check_env


def _base(**over):
    report = {
        "gpus": [],
        "ctranslate2_cuda_devices": None,
        "ram_gb": 16,
        "packages": {"pyannote.audio": False},
        "hf_token_set": False,
    }
    report.update(over)
    return report


def test_cpu_only_picks_int8_cpu():
    d = check_env.derive_defaults(_base())
    assert d["whisper"]["device"] == "cpu"
    assert d["whisper"]["compute_type"] == "int8"
    assert d["whisper"]["model"] == "medium"
    assert check_env.derive_defaults(_base(ram_gb=15.7))["whisper"]["model"] == "medium"
    assert d["diarization_backend"] == "basic"


def test_low_ram_cpu_uses_small_model():
    assert check_env.derive_defaults(_base(ram_gb=8))["whisper"]["model"] == "small"


def test_big_gpu_with_cuda_picks_large_v3():
    d = check_env.derive_defaults(
        _base(gpus=[{"name": "RTX 4070", "vram_gb": 12, "driver": "x"}], ctranslate2_cuda_devices=1)
    )
    assert d["whisper"] == {**d["whisper"], "model": "large-v3", "device": "cuda"}


def test_gpu_without_cuda_libs_falls_back_to_cpu_and_says_why():
    d = check_env.derive_defaults(
        _base(gpus=[{"name": "RTX 3060", "vram_gb": 12, "driver": "x"}], ctranslate2_cuda_devices=0)
    )
    assert d["whisper"]["device"] == "cpu"
    assert "CTranslate2 can't use CUDA" in d["whisper"]["reason"]


def test_pyannote_needs_package_and_token():
    assert (
        check_env.derive_defaults(_base(packages={"pyannote.audio": True}))["diarization_backend"]
        == "basic"
    )
    d = check_env.derive_defaults(_base(packages={"pyannote.audio": True}, hf_token_set=True))
    assert d["diarization_backend"] == "pyannote"


def test_main_writes_report(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["clipforge-check"])
    check_env.main()
    assert "Whisper:" in capsys.readouterr().out
    loaded = json.loads(check_env.report_path().read_text())
    assert loaded["defaults"]["whisper"]["device"] in ("cpu", "cuda")
    assert "ffmpeg" in loaded["tools"]
    assert isinstance(loaded["problems"], list)


def test_rtx_3050_6gb_gets_int8_large_v3_and_12k_llm_context():
    d = check_env.derive_defaults(
        _base(
            gpus=[{"name": "NVIDIA GeForce RTX 3050", "vram_gb": 6.0, "driver": "x"}],
            ctranslate2_cuda_devices=1,
        )
    )
    assert d["whisper"]["model"] == "large-v3"
    assert d["whisper"]["compute_type"] == "int8_float16"
    assert d["ollama_num_ctx"] == 12288


def test_cpu_only_gets_smaller_llm_context():
    assert check_env.derive_defaults(_base())["ollama_num_ctx"] == 8192
