"""Export pack: video, transcript, shot list, captions, copy, QA report, manifest, zip.

Exports only clips that passed the verification gate, whose B-roll was checked against the
current rules, and that have a copy pack. FAKE_MODELS runs can't pass the gate; for plumbing
tests only, they export into a folder marked UNVERIFIED-TEST.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from clipforge import __version__
from clipforge.core.copywriter import CopyPack
from clipforge.editspec import EditSpec
from clipforge.qa import QAReport


class ExportBlocked(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s)[:n].strip("-") or "clip"


def export_gate(
    verified: bool, qa: list[dict], reverify: QAReport, copy: CopyPack | None, has_render: bool
) -> list[str]:
    reasons = []
    if not has_render:
        reasons.append("clip isn't rendered")
    if not verified:
        failed = [
            f"{r['step']}: {c['name']}"
            + (f" @ {c['at_seconds']:.2f} s" if c.get("at_seconds") is not None else "")
            for r in qa
            for c in r["checks"]
            if not c["passed"] and c.get("severity", "error") == "error"
        ]
        reasons.append("verification gate failed: " + "; ".join(failed[:6]))
    if not reverify.passed:
        reasons += [f"B-roll: {c.name} ({c.detail})" for c in reverify.failures]
    if copy is None:
        reasons.append("no copy pack yet")
    return reasons


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def shot_list(spec: EditSpec) -> str:
    rows = []
    for off, p in zip(spec.out_offsets(), spec.pieces, strict=True):
        rows.append([f"{off:.3f}", f"{off + p.duration:.3f}", "speaker", "podcast", "", "",
                     f"episode {p.src_start:.3f}-{p.src_end:.3f}", ""])  # fmt: skip
    for s in spec.broll:
        if s.status == "filled" and s.choice:
            c = s.choice
            rows.append([f"{s.out_start:.3f}", f"{s.out_end:.3f}", "b-roll", c.source, c.title,
                         c.page_url, c.license, c.approved_by])  # fmt: skip
        elif s.status == "weak":
            rows.append([f"{s.out_start:.3f}", f"{s.out_end:.3f}", "WEAK", "", s.weak_reason,
                         "", "", ""])  # fmt: skip
    if spec.host_insert and spec.host_insert_enabled:
        h = spec.host_insert
        rows.append([f"{h.out_start:.3f}", f"{h.out_end:.3f}", "host reaction", "podcast",
                     h.reason, "", f"episode {h.src_start:.3f}-{h.src_end:.3f}", ""])  # fmt: skip
    rows.sort(key=lambda r: float(r[0]))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "out_start",
            "out_end",
            "type",
            "source",
            "title_or_note",
            "url",
            "license_or_source_time",
            "approved_by",
        ]
    )
    w.writerows(rows)
    return buf.getvalue()


def transcript_text(spec: EditSpec) -> str:
    return " ".join(c.text for c in spec.captions)


def copy_markdown(pack: CopyPack) -> str:
    return (
        f"# {pack.title}\n\n"
        f"## YouTube description\n\n{pack.youtube_description}\n\n"
        f"## Instagram caption\n\n{pack.instagram_caption}\n\n{' '.join(pack.instagram_tags)}\n\n"
        f"## X post\n\n{pack.x_post}\n"
    )


def write_pack(
    out_root: Path,
    project_name: str,
    rank: int,
    spec: EditSpec,
    render: Path,
    copy: CopyPack,
    qa: list[dict],
    captions_dir: Path,
    *,
    unverified_test: bool = False,
) -> tuple[Path, Path]:
    name = f"{rank:02d}-{slug(copy.title)}" + ("-UNVERIFIED-TEST" if unverified_test else "")
    folder = out_root / slug(project_name, 30) / name
    tmp = folder.with_name(folder.name + ".part")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    shutil.copy2(render, tmp / "video.mp4")
    (tmp / "transcript.txt").write_text(transcript_text(spec) + "\n", encoding="utf-8")
    (tmp / "transcript.json").write_text(
        json.dumps([c.model_dump() for c in spec.captions], indent=2), encoding="utf-8"
    )
    (tmp / "shot_list.csv").write_text(shot_list(spec), encoding="utf-8")
    for name_ in ("captions.srt", "captions.ass"):
        if (captions_dir / name_).exists():
            shutil.copy2(captions_dir / name_, tmp / name_)
    (tmp / "copy.md").write_text(copy_markdown(copy), encoding="utf-8")
    (tmp / "copy.json").write_text(copy.model_dump_json(indent=2), encoding="utf-8")
    (tmp / "qa.json").write_text(json.dumps(qa, indent=2), encoding="utf-8")
    from clipforge.core.learn import load_insights
    from clipforge.core.posting import checklist, ics, suggest_time

    when, why = suggest_time(load_insights())
    (tmp / "posting.md").write_text(checklist(copy, when, why), encoding="utf-8")
    (tmp / "post.ics").write_text(
        ics(copy.title, when, copy.x_post, f"{slug(project_name)}-{rank}"), encoding="utf-8"
    )
    manifest = {
        "clipforge_version": __version__,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "verified": not unverified_test,
        "unverified_test_export": unverified_test,
        "duration_seconds": spec.duration,
        "resolution": f"{spec.width}x{spec.height}",
        "fps": spec.fps,
        "files": {p.name: _sha256(p) for p in sorted(tmp.iterdir()) if p.is_file()},
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if folder.exists():
        shutil.rmtree(folder)  # re-export of the same clip replaces its own previous pack
    tmp.replace(folder)
    zpath = folder.with_suffix(".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.iterdir()):
            z.write(p, f"{folder.name}/{p.name}")
    return folder, zpath
