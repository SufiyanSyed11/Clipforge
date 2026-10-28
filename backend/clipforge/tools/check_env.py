"""`clipforge-check`: inspect this machine and derive hardware-dependent defaults.

Writes data/env_report.json. Transcription and diarization read it when their settings are
"auto", so GPU components are only chosen when a usable GPU is actually present.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from clipforge.settings import get_settings

REPORT_NAME = "env_report.json"


def _run(cmd: list[str], timeout: float = 15) -> str | None:
    exe = shutil.which(cmd[0])
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout or out.stderr).strip() or None


def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMSTAT(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMSTAT()
            st.dwLength = ctypes.sizeof(MEMSTAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 1024**3, 1)
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * size / 1024**3, 1)
    except (OSError, ValueError, AttributeError):
        return None


def _gpus() -> list[dict[str, Any]]:
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus: list[dict[str, Any]] = []
    if not out or "failed" in out.lower():
        return gpus
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                vram = round(float(parts[1]) / 1024, 1)
            except ValueError:
                vram = None
            gpus.append({"name": parts[0], "vram_gb": vram, "driver": parts[2]})
    return gpus


def _ctranslate2_cuda_devices() -> int | None:
    """How many CUDA devices faster-whisper's engine can actually use (None if not installed)."""
    if not _has_module("ctranslate2"):
        return None
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _first_line(text: str | None) -> str | None:
    return text.splitlines()[0] if text else None


def derive_defaults(report: dict[str, Any]) -> dict[str, Any]:
    gpus = report["gpus"]
    cuda_ok = (report["ctranslate2_cuda_devices"] or 0) > 0
    vram = max((g["vram_gb"] or 0) for g in gpus) if gpus else 0
    ram = report["ram_gb"] or 0
    if gpus and cuda_ok and vram >= 8:
        whisper = {"model": "large-v3", "device": "cuda", "compute_type": "float16"}
        reason = f"NVIDIA GPU with {vram} GB VRAM and CUDA available to CTranslate2"
    elif gpus and cuda_ok and vram >= 5.5:
        # e.g. RTX 3050 6 GB: large-v3 in int8 (~2 GB) leaves headroom; float16 (~4.5 GB
        # with activations) is too tight next to the desktop and a briefly-loaded local LLM.
        whisper = {"model": "large-v3", "device": "cuda", "compute_type": "int8_float16"}
        reason = f"NVIDIA GPU with {vram} GB VRAM: large-v3 in int8 to fit comfortably"
    elif gpus and cuda_ok:
        whisper = {"model": "medium", "device": "cuda", "compute_type": "int8_float16"}
        reason = f"NVIDIA GPU with only {vram} GB VRAM"
    else:
        # "16 GB" machines report ~15.7 GB usable.
        model = "medium" if ram >= 15 else "small"
        whisper = {"model": model, "device": "cpu", "compute_type": "int8"}
        if gpus and not cuda_ok:
            reason = (
                "NVIDIA GPU found but CTranslate2 can't use CUDA (install faster-whisper and "
                "the CUDA 12 cuBLAS/cuDNN libraries, then re-run clipforge-check)"
            )
        else:
            reason = f"No usable NVIDIA GPU; CPU with {ram} GB RAM"
    pyannote_ok = report["packages"].get("pyannote.audio") and report["hf_token_set"]
    # Local LLM context: qwen3:4b (~2.5 GB) + KV cache (~0.14 MB/token at f16).
    if gpus and vram >= 8:
        num_ctx, ctx_reason = 16384, f"{vram} GB VRAM"
    elif gpus and vram >= 5.5:
        num_ctx, ctx_reason = 12288, f"{vram} GB VRAM: model + 12k context ≈ 4.3 GB"
    else:
        num_ctx, ctx_reason = 8192, "no usable GPU: smaller context keeps CPU inference practical"
    return {
        "whisper": {**whisper, "reason": reason},
        "diarization_backend": "pyannote" if pyannote_ok else "basic",
        "ollama_num_ctx": num_ctx,
        "ollama_num_ctx_reason": ctx_reason,
    }


def build_report() -> dict[str, Any]:
    settings = get_settings()
    du = shutil.disk_usage(settings.data_dir)
    packages = {
        name: _has_module(mod)
        for name, mod in [
            ("faster-whisper", "faster_whisper"),
            ("yt-dlp", "yt_dlp"),
            ("anthropic", "anthropic"),
            ("pyannote.audio", "pyannote.audio"),
            ("scikit-learn", "sklearn"),
            ("scipy", "scipy"),
        ]
    }
    report: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "cpu": platform.processor() or None,
        "cpu_cores": os.cpu_count(),
        "ram_gb": _ram_gb(),
        "disk_free_gb": round(du.free / 1024**3, 1),
        "data_dir": str(settings.data_dir),
        "gpus": _gpus(),
        "ctranslate2_cuda_devices": _ctranslate2_cuda_devices(),
        "tools": {
            "ffmpeg": _first_line(_run(["ffmpeg", "-version"])),
            "ffprobe": _first_line(_run(["ffprobe", "-version"])),
            "yt-dlp": _first_line(_run(["yt-dlp", "--version"])),
            "node": _first_line(_run(["node", "--version"])),
        },
        "packages": packages,
        "hf_token_set": bool(settings.hf_token),
        "anthropic_key_set": bool(
            settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        ),
    }
    report["defaults"] = derive_defaults(report)
    report["llm"] = _llm_status(settings, report["defaults"])
    report["problems"] = problems(report)
    return report


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:  # parent package of a dotted name is missing
        return False


def _llm_status(settings, defaults: dict[str, Any]) -> dict[str, Any]:
    from clipforge.llm.factory import get_text_provider

    try:
        provider = get_text_provider(settings, num_ctx_default=defaults["ollama_num_ctx"])
        return provider.check().to_dict()
    except Exception as e:  # report, never crash the check
        return {"ok": False, "provider": settings.llm_provider, "detail": str(e), "fix": None}


def problems(report: dict[str, Any]) -> list[str]:
    out = []
    if not report["tools"]["ffmpeg"] or not report["tools"]["ffprobe"]:
        out.append("ffmpeg/ffprobe not found on PATH (required)")
    if not report["packages"]["faster-whisper"]:
        out.append("faster-whisper not installed: pip install -e backend[pipeline]")
    if not report["packages"]["yt-dlp"] and not report["tools"]["yt-dlp"]:
        out.append("yt-dlp not installed (needed for URL ingest)")
    llm = report.get("llm") or {}
    if llm and not llm.get("ok"):
        out.append(f"Clip picker: {llm.get('detail')} {llm.get('fix') or ''}".strip())
    if report["disk_free_gb"] < 20:
        out.append(f"Only {report['disk_free_gb']} GB free disk; episodes + models need ~20 GB")
    return out


def report_path() -> Path:
    return get_settings().data_dir / REPORT_NAME


def load_report() -> dict[str, Any] | None:
    p = report_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Check hardware and tools for ClipForge")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args()
    report = build_report()
    report_path().write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
        return
    d = report["defaults"]
    print(f"OS:        {report['os']}")
    print(f"CPU:       {report['cpu'] or 'unknown'} ({report['cpu_cores']} cores)")
    print(f"RAM:       {report['ram_gb']} GB   Free disk: {report['disk_free_gb']} GB")
    gpus = ", ".join(f"{g['name']} ({g['vram_gb']} GB)" for g in report["gpus"]) or "none found"
    print(f"GPU:       {gpus}")
    print(f"CUDA for whisper: {report['ctranslate2_cuda_devices']}")
    for k, v in report["tools"].items():
        print(f"{k:9}  {v or 'MISSING'}")
    w = d["whisper"]
    print(f"\nWhisper:     {w['model']} on {w['device']} ({w['compute_type']}) - {w['reason']}")
    print(f"Diarization: {d['diarization_backend']}")
    llm = report["llm"]
    print(f"Clip picker: {'OK' if llm['ok'] else 'NOT READY'} - {llm['detail']}")
    print(f"LLM context: {d['ollama_num_ctx']} tokens ({d['ollama_num_ctx_reason']})")
    if report["problems"]:
        print("\nTo fix:")
        for p in report["problems"]:
            print(f"  - {p}")
    print(f"\nReport written to {report_path()}")


if __name__ == "__main__":
    main()
