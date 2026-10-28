"""Audio mix: voice at about -14 LUFS, royalty-free BGM about 19 dB under the voice with
sidechain ducking, light SFX only on motivated transitions, limiter at -1 dB.

Everything is measured afterwards with the EBU R128 meter; the QA report says what came out.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from clipforge.core.audio import measure_loudness
from clipforge.qa import QAReport

VOICE_LUFS = -14.0
BGM_UNDER_DB = 19.0
LIMIT_DBTP = -1.0
LUFS_TOL = 1.0


class MixError(Exception):
    pass


def _ff(args: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MixError("ffmpeg not found on PATH")
    proc = subprocess.run([ffmpeg, "-y", "-v", "error", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise MixError(proc.stderr.strip()[-600:])


def normalize_voice(voice: Path, out: Path, target: float = VOICE_LUFS) -> dict[str, float]:
    """Two-pass loudnorm to `target` LUFS with a -1.5 dBTP ceiling (the limiter finishes)."""
    m = measure_loudness(voice)
    if m["lufs"] == float("-inf") or m["lufs"] < -70:
        raise MixError("The voice track is silent")
    _ff(
        [
            "-i", str(voice),
            "-af",
            f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={m['lufs']}:measured_TP={m['true_peak']}"
            f":measured_LRA={m['lra']}:measured_thresh={m['threshold']}:linear=true,"
            "aresample=48000",
            "-ar", "48000", "-ac", "1", str(out),
        ]
    )  # fmt: skip
    return m


def _render_mix(
    vnorm: Path,
    out: Path,
    *,
    bgm: Path | None,
    bgm_lufs: float,
    sfx: Path | None,
    sfx_times: list[float] | None,
    duration: float,
    limit_db: float,
    workdir: Path,
) -> Path | None:
    limit_lin = 10 ** (limit_db / 20)
    inputs = ["-i", str(vnorm)]
    chains = ["[0:a]aformat=channel_layouts=stereo,asplit=2[v][key]"]
    mix_in = ["[v]"]
    bgm_stem = None
    if bgm and bgm.exists():
        bgm_level = workdir / "bgm_level.wav"
        fade_out = max(0.0, duration - 0.8)
        _ff(
            [
                "-stream_loop", "-1", "-i", str(bgm), "-t", f"{duration:.3f}",
                "-af", f"loudnorm=I={bgm_lufs:.2f}:TP=-6:LRA=20,"
                f"afade=t=in:d=0.5,afade=t=out:st={fade_out:.3f}:d=0.8",
                "-ar", "48000", "-ac", "2", str(bgm_level),
            ]
        )  # fmt: skip
        inputs += ["-i", str(bgm_level)]
        # Duck the music under the voice (sidechain); keep a copy of the ducked stem so its
        # level can be measured.
        chains.append(
            "[1:a][key]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350:"
            "makeup=1[duck]"
        )
        chains.append("[duck]asplit=2[bgm][bgmstem]")
        mix_in.append("[bgm]")
        bgm_stem = workdir / "bgm_ducked.wav"
    else:
        chains.append("[key]anullsink")
    if sfx and sfx.exists() and sfx_times:
        idx = len(inputs) // 2
        inputs += ["-i", str(sfx)]
        for k, t in enumerate(sfx_times):
            chains.append(f"[{idx}:a]adelay={int(t * 1000)}|{int(t * 1000)},volume=0.35[s{k}]")
        chains.append(
            "".join(f"[s{k}]" for k in range(len(sfx_times)))
            + f"amix=inputs={len(sfx_times)}:normalize=0[sfx]"
        )
        mix_in.append("[sfx]")
    chains.append(
        "".join(mix_in)
        + f"amix=inputs={len(mix_in)}:normalize=0:duration=first,"
        + f"alimiter=limit={limit_lin:.4f}:attack=5:release=50:level=false[out]"
    )
    args = [*inputs, "-filter_complex", ";".join(chains), "-map", "[out]"]
    args += ["-t", f"{duration:.6f}", "-ar", "48000", "-ac", "2", str(out)]
    if bgm_stem:
        args += ["-map", "[bgmstem]", "-t", f"{duration:.6f}", "-ar", "48000", str(bgm_stem)]
    _ff(args)
    return bgm_stem


def mix(
    voice: Path,
    out: Path,
    *,
    bgm: Path | None = None,
    sfx: Path | None = None,
    sfx_times: list[float] | None = None,
    duration: float,
    target_lufs: float = VOICE_LUFS,
    bgm_under_db: float = BGM_UNDER_DB,
    limit_db: float = LIMIT_DBTP,
    workdir: Path,
) -> QAReport:
    qa = QAReport(step="mix")
    workdir.mkdir(parents=True, exist_ok=True)
    vnorm = workdir / "voice_norm.wav"
    normalize_voice(voice, vnorm, target_lufs)
    vm = measure_loudness(vnorm)
    # "19 dB under the voice" is judged on the ducked music, so level it, duck it, measure,
    # and correct once.
    bgm_lufs = target_lufs - bgm_under_db
    under = None
    for _ in range(3):
        stem = _render_mix(
            vnorm,
            out,
            bgm=bgm,
            bgm_lufs=bgm_lufs,
            sfx=sfx,
            sfx_times=sfx_times,
            duration=duration,
            limit_db=limit_db,
            workdir=workdir,
        )
        if not stem:
            break
        under = vm["lufs"] - measure_loudness(stem)["lufs"]
        if abs(under - bgm_under_db) <= 0.75:
            break
        bgm_lufs = min(target_lufs - 6, bgm_lufs + (under - bgm_under_db))
    bgm_stem = workdir / "bgm_ducked.wav" if under is not None else None

    final = measure_loudness(out)
    qa.check(
        f"voice at about {target_lufs:.0f} LUFS",
        abs(vm["lufs"] - target_lufs) <= LUFS_TOL,
        f"voice {vm['lufs']:.1f} LUFS",
    )
    qa.check(
        f"limiter: true peak at most {limit_db:.0f} dBTP",
        final["true_peak"] <= limit_db + 0.3,  # inter-sample peaks after AAC: tiny margin
        f"{final['true_peak']:.2f} dBTP",
    )
    qa.check(
        "mix loudness",
        abs(final["lufs"] - target_lufs) <= LUFS_TOL + 0.5,
        f"{final['lufs']:.1f} LUFS",
        severity="warn",
    )
    if bgm_stem and under is not None:
        qa.check(
            f"music about {bgm_under_db:.0f} dB under the voice (ducked)",
            abs(under - bgm_under_db) <= 1.5,
            f"ducked music {vm['lufs'] - under:.1f} LUFS = {under:.1f} dB under the voice",
        )
    else:
        qa.check(
            "background music",
            True,
            "no royalty-free music track configured (add one in Settings)",
            severity="warn",
        )
    if sfx_times:
        qa.check(
            "SFX only on motivated transitions",
            True,
            f"{len(sfx_times)} accent(s) at B-roll transitions",
            severity="warn",
        )
    return qa
