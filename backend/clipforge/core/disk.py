"""Disk usage and cleanup for the data folder.

Long episodes are big (a 2 h 1080p source is several GB, plus 48 kHz WAVs), and the owner's
PC has limited free space. This module reports where the space goes, refuses to start a
download or render when free space is below `MIN_FREE_GB`, and deletes only files that the
pipeline can recreate. Source media, renders and export packs are never touched by
"intermediates" cleanup; removing a whole project needs an explicit confirmation.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# Files the pipeline rebuilds on demand (audio extraction, per-clip voice/mix stems).
PROJECT_INTERMEDIATES = ("audio48k.wav", "audio16k.wav")
CLIP_INTERMEDIATES = ("voice.wav", "mix.wav")
CLIP_INTERMEDIATE_DIRS = ("mix",)


class LowDiskSpace(Exception):
    pass


@dataclass
class Usage:
    total_gb: float
    free_gb: float
    min_free_gb: float
    projects_gb: float
    intermediates_gb: float
    broll_cache_gb: float
    exports_gb: float
    per_project: list[dict]

    def to_dict(self) -> dict:
        return self.__dict__ | {"low": self.free_gb < self.min_free_gb}


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    if not p.is_dir():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _gb(n: int) -> float:
    return round(n / 1024**3, 3)


def intermediates(project_dir: Path) -> list[Path]:
    out = [project_dir / n for n in PROJECT_INTERMEDIATES if (project_dir / n).exists()]
    for clip in (
        sorted((project_dir / "clips").glob("*")) if (project_dir / "clips").is_dir() else []
    ):
        out += [clip / n for n in CLIP_INTERMEDIATES if (clip / n).exists()]
        out += [clip / n for n in CLIP_INTERMEDIATE_DIRS if (clip / n).is_dir()]
    return out


def usage(data_dir: Path, min_free_gb: float) -> Usage:
    data_dir.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(data_dir)
    per = []
    inter_total = 0
    proj_total = 0
    for pdir in (
        sorted((data_dir / "projects").glob("*")) if (data_dir / "projects").is_dir() else []
    ):
        if not pdir.is_dir():
            continue
        size = _size(pdir)
        inter = sum(_size(p) for p in intermediates(pdir))
        proj_total += size
        inter_total += inter
        try:
            pid = int(pdir.name)
        except ValueError:
            continue
        per.append({"project_id": pid, "gb": _gb(size), "intermediates_gb": _gb(inter)})
    return Usage(
        total_gb=_gb(du.total),
        free_gb=_gb(du.free),
        min_free_gb=min_free_gb,
        projects_gb=_gb(proj_total),
        intermediates_gb=_gb(inter_total),
        broll_cache_gb=_gb(_size(data_dir / "broll_cache")),
        exports_gb=_gb(_size(data_dir / "exports")),
        per_project=per,
    )


def require_free(path: Path, min_free_gb: float, need_gb: float = 0.0, what: str = "") -> None:
    """Fail before a long step instead of halfway through it with a full disk."""
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free / 1024**3
    if free - need_gb < min_free_gb:
        need = f" plus about {need_gb:.1f} GB for this step" if need_gb else ""
        raise LowDiskSpace(
            f"Not enough free disk space{' for ' + what if what else ''}: {free:.1f} GB free, "
            f"ClipForge keeps at least {min_free_gb:.1f} GB free{need}. Free some space "
            "(System page -> Storage clears re-creatable files) or lower MIN_FREE_GB in .env."
        )


def _remove(p: Path) -> int:
    n = _size(p)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)
    return n


def clean_intermediates(project_dir: Path) -> int:
    """Delete re-creatable files of one project. Returns bytes freed."""
    return sum(_remove(p) for p in intermediates(project_dir))


def clean_broll_cache(data_dir: Path) -> int:
    cache = data_dir / "broll_cache"
    if not cache.is_dir():
        return 0
    return sum(_remove(p) for p in list(cache.iterdir()))


def safe_child(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
