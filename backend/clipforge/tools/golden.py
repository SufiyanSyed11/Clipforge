"""`clipforge-golden`: run the full pipeline on the owner's three golden-sample episodes.

    clipforge-golden init           # writes golden/episodes.json to fill in
    clipforge-golden run            # every episode: ingest -> pick -> build every clip
    clipforge-golden run --pick-only    # Phase 1 only (transcribe + pick)

Results go to docs/reports/golden/<episode id>/ (transcript, clips, QA) plus
golden-summary.md with the quality-bar checks per clip. Clips are built through the same job
queue the worker uses, so stop the worker (scripts/dev) while this runs or both will compete.
Nothing is written as "passed" by this tool beyond what the automated checks measured: the
owner still watches and listens, and fills in "what is weak". It refuses to run with
FAKE_MODELS.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from clipforge.db import Clip, Job, Project, session_scope, utcnow
from clipforge.jobs import queue
from clipforge.jobs.registry import JobContext
from clipforge.pipeline import clip_tasks, tasks  # noqa: F401  (registers the job kinds)
from clipforge.settings import REPO_ROOT, get_settings

EPISODES = REPO_ROOT / "golden" / "episodes.json"
REPORT_DIR = REPO_ROOT / "docs" / "reports" / "golden"

TEMPLATE = {
    "_how_to": (
        "Add exactly three episodes you have the rights to clip. 'source' is a local file path "
        "or a URL. Set rights_confirmed to true only if you have the rights or permission. "
        "Try to include one with noisy audio or crosstalk and one multi-camera show. "
        "host_speaker is optional (e.g. 'S1'); leave it null to use the app's suggestion."
    ),
    "episodes": [
        {
            "id": "ep1",
            "title": "",
            "source": "",
            "rights_confirmed": False,
            "notes": "e.g. clean studio audio",
            "host_speaker": None,
        },
        {
            "id": "ep2",
            "title": "",
            "source": "",
            "rights_confirmed": False,
            "notes": "e.g. noisy audio / crosstalk",
            "host_speaker": None,
        },
        {
            "id": "ep3",
            "title": "",
            "source": "",
            "rights_confirmed": False,
            "notes": "e.g. multi-camera with a TV wall",
            "host_speaker": None,
        },
    ],
}


class _PrintContext(JobContext):
    def log(self, message: str, progress: float | None = None, level: str = "info") -> None:
        print(f"  {message}")

    def heartbeat(self) -> None:
        pass


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


# Quality bar (CLAUDE.md, "Golden-sample quality bar") -> the gate checks that measure it.
QUALITY_BAR: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "zero clipped words",
        [
            ("verify", "no word missing at a join or edge (zero clipped words)"),
            ("verify", "re-transcription matches the intended words"),
            ("verify", "join at"),
            ("verify", "first 0.3 s"),
            ("verify", "last 0.45 s"),
            ("verify", "re-transcription by a real model"),
        ],
    ),
    ("zero profanity leaks", [("verify", "no profanity audible in the cut")]),
    ("voice at -14 LUFS", [("mix", "voice at about -14 LUFS")]),
    ("BGM under the voice", [("mix", "music about 19 dB under the voice")]),
    ("limiter at -1 dB", [("mix", "limiter")]),
    (
        "audio duration == video duration",
        [
            ("render-verify", "audio duration equals video duration"),
            ("render-verify", "frame count matches"),
        ],
    ),
    ("B-roll checked (no text/logos)", [("broll", "footage checked")]),
]


def quality_bar(qa: list[dict]) -> list[tuple[str, str, str]]:
    """(item, PASS/FAIL/not measured, detail) for one clip, from its QA reports."""
    out = []
    for item, probes in QUALITY_BAR:
        hits = [
            c
            for rep in qa
            for c in rep["checks"]
            for step, prefix in probes
            if rep["step"] == step and prefix in c["name"]
        ]
        if not hits:
            out.append((item, "not measured", "-"))
            continue
        bad = [c for c in hits if not c["passed"]]
        detail = "; ".join(
            f"{c['name']}: {c['detail']}"
            + (f" @ {c['at_seconds']:.2f} s" if c.get("at_seconds") is not None else "")
            for c in bad[:3]
        )
        out.append((item, "FAIL" if bad else "PASS", detail or f"{len(hits)} check(s)"))
    return out


def drain_queue(project_id: int) -> None:
    """Run this project's queued jobs (analysis, clip builds, copy) like the worker does."""
    from clipforge.jobs.worker import run_one

    while True:
        with session_scope() as s:
            pending = (
                s.query(Job)
                .filter(Job.project_id == project_id, Job.status.in_(("queued", "running")))
                .count()
            )
        if not pending:
            return
        job_id = run_one("golden")
        if job_id is None:
            return  # only another worker's running jobs are left
        with session_scope() as s:
            j = s.get(Job, job_id)
            print(f"  job {j.kind} #{j.id}: {j.status}" + (f" ({j.error})" if j.error else ""))


def run_episode(ep: dict, n: int, pick_only: bool = False) -> dict:
    out = REPORT_DIR / ep["id"]
    out.mkdir(parents=True, exist_ok=True)
    src = ep["source"]
    is_url = src.startswith(("http://", "https://"))
    with session_scope() as s:
        p = Project(
            name=f"golden:{ep['id']} {ep.get('title', '')}".strip(),
            source_type="url" if is_url else "upload",
            source=src if is_url else str(Path(src).expanduser().resolve()),
            rights_confirmed=bool(ep.get("rights_confirmed")),
            rights_confirmed_at=utcnow() if ep.get("rights_confirmed") else None,
            clip_count=n,
        )
        s.add(p)
        s.flush()
        pid = p.id
    ctx = _PrintContext(job_id=0, kind="golden", payload={}, project_id=pid)
    started = time.time()
    t, qa = tasks.run_ingest_and_transcribe(pid, ctx)
    transcribe_s = time.time() - started
    with session_scope() as s:
        p = s.get(Project, pid)
        suggestion = p.host_suggestion
        host = ep.get("host_speaker") or (suggestion or {}).get("speaker")
        p.host_speaker, p.host_confirmed = host, True
        speakers = p.speakers
        media_info = p.media_info
    host_source = (
        "set in episodes.json" if ep.get("host_speaker") else "app suggestion (unreviewed)"
    )
    if not pick_only and (media_info or {}).get("has_video"):
        # Queued before the clip builds, so faces/speakers are analysed first.
        queue.enqueue("analyze_video", {}, project_id=pid)
    pick_result = tasks.pick_clips_task(ctx)
    pdir = Path(tasks.get_storage().project_dir(pid))
    clips = json.loads((pdir / "clips.json").read_text(encoding="utf-8"))
    for name in ("transcript.json", "transcript.txt", "clips.json"):
        (out / name).write_bytes((pdir / name).read_bytes())
    all_qa = [r.to_dict() for r in qa] + [r.to_dict() for r in pick_result.qa]
    (out / "qa.json").write_text(json.dumps(all_qa, indent=2), encoding="utf-8")
    built: list[dict] = []
    if pick_only:
        with session_scope() as s:
            for j in s.query(Job).filter(Job.project_id == pid, Job.status == "queued"):
                j.status, j.error = "cancelled", "golden run with --pick-only"
    else:
        started = time.time()
        drain_queue(pid)
        build_s = time.time() - started
        with session_scope() as s:
            for c in s.query(Clip).filter(Clip.project_id == pid).order_by(Clip.rank):
                built.append(
                    {
                        "rank": c.rank,
                        "status": c.status,
                        "verified": c.verified,
                        "error": c.error,
                        "render": c.render_path,
                        "copy_title": (c.copy_pack or {}).get("title"),
                        "quality_bar": quality_bar(c.qa or []),
                    }
                )
                (out / f"clip{c.rank:02d}-qa.json").write_text(
                    json.dumps(c.qa or [], indent=2), encoding="utf-8"
                )
        print(f"  built {len(built)} clip(s) in {build_s:.0f} s")
    return {
        "built": built,
        "id": ep["id"],
        "title": ep.get("title"),
        "notes": ep.get("notes"),
        "project_id": pid,
        "duration_min": round(t.duration / 60, 1),
        "transcribe_seconds": round(transcribe_s),
        "words": len(t.words),
        "speakers": speakers,
        "media_info": media_info,
        "host": host,
        "host_source": host_source,
        "host_suggestion": suggestion,
        "clips": clips["clips"],
        "rejected": clips["rejected"],
        "qa": all_qa,
    }


def summary_markdown(results: list[dict], env: dict | None) -> str:
    s = get_settings()
    lines = [
        "# Golden run",
        "",
        f"Run at {datetime.now().isoformat(timespec='minutes')}. "
        f"Clip picker `{s.llm_provider}`, prompt `{s.picker_prompt_version}`.",
    ]
    if env:
        w = env["defaults"]["whisper"]
        lines.append(
            f"Machine: {env['os']}, {env['cpu_cores']} cores, {env['ram_gb']} GB RAM, "
            f"GPU: {', '.join(g['name'] for g in env['gpus']) or 'none'}. "
            f"Whisper {w['model']} on {w['device']}."
        )
    for r in results:
        lines += ["", f"## {r['id']}: {r.get('title') or ''}", ""]
        if r.get("error"):
            lines.append(f"**Failed:** {r['error']}")
            continue
        lines.append(
            f"{r['duration_min']} min, transcribed in {r['transcribe_seconds']} s, "
            f"{r['words']} words. Notes: {r.get('notes') or '-'}"
        )
        stats = (r.get("speakers") or {}).get("stats", [])
        lines.append(
            "Speakers: "
            + ", ".join(f"{x['speaker']} {x['talk_share']:.0%}" for x in stats)
            + f". Host: {r['host']} ({r['host_source']})."
        )
        failing = [
            f"{rep['step']}: {c['name']} ({c['detail']})"
            for rep in r["qa"]
            for c in rep["checks"]
            if not c["passed"]
        ]
        lines.append("QA items to look at: " + ("; ".join(failing) if failing else "none"))
        lines += ["", "| # | Window | Length | Score | Hook | Flags |", "|---|---|---|---|---|---|"]
        for c in r["clips"]:
            p = c["plan"]
            window = " + ".join(f"{_fmt(x['start'])}-{_fmt(x['end'])}" for x in p["segments"])
            flags = "; ".join(c["flags"] + [f"{f['type']}" for f in p["risk_flags"]]) or "-"
            hook = p["hook"].replace("|", "/")[:90]
            lines.append(
                f"| {c['rank']} | {window} | {c['duration']:.1f} s | {p['virality_score']} | "
                f"{hook} | {flags} |"
            )
        if r["rejected"]:
            lines.append("")
            lines.append(
                "Rejected by the hard rules: "
                + "; ".join(f"#{x['model_rank']} {x['reason']}" for x in r["rejected"])
            )
        if r.get("built"):
            lines += ["", "### Built clips: quality bar (automated checks)", ""]
            for b in r["built"]:
                state = "verified" if b["verified"] else b["status"]
                lines.append(
                    f"**Clip #{b['rank']}** — {state}"
                    + (f" — {b['error']}" if b.get("error") else "")
                    + (f" — title: {b['copy_title']}" if b.get("copy_title") else "")
                    + (f"  \n`{b['render']}`" if b.get("render") else "")
                )
                lines += ["", "| Quality bar | Result | Detail |", "|---|---|---|"]
                for item, res, detail in b["quality_bar"]:
                    lines.append(f"| {item} | {res} | {detail.replace('|', '/')[:200]} |")
                lines.append("")
        lines += ["", "### Owner review: what is weak", "", "- (fill in after watching/listening)"]
    lines += [
        "",
        "## Not verified by this run",
        "",
        "- Clip quality is the owner's judgement; the QA checks only cover structure and rules.",
        "- B-roll 'no text/logos' is only proven by a vision model or the owner's approval of "
        "each contact sheet; 'PASS' means every shot was approved under the current rules.",
        "- The quality bar is met only if every built clip shows PASS on every row AND the "
        "owner found nothing wrong watching and listening.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    run = sub.add_parser("run")
    run.add_argument("--episode", help="run only this episode id")
    run.add_argument("-n", type=int, default=8)
    run.add_argument("--pick-only", action="store_true", help="transcribe and pick only")
    args = ap.parse_args(argv)

    if args.cmd == "init":
        if EPISODES.exists():
            print(f"{EPISODES} already exists; not overwriting it.")
            return 0
        EPISODES.parent.mkdir(parents=True, exist_ok=True)
        EPISODES.write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")
        print(f"Fill in {EPISODES}, then run: clipforge-golden run")
        return 0

    if get_settings().fake_models:
        print("FAKE_MODELS is on. Golden runs must use real models; set FAKE_MODELS=false.")
        return 2
    if not EPISODES.exists():
        print("No golden/episodes.json yet. Run: clipforge-golden init")
        return 2
    episodes = json.loads(EPISODES.read_text(encoding="utf-8"))["episodes"]
    if args.episode:
        episodes = [e for e in episodes if e["id"] == args.episode]
    ready = [e for e in episodes if e.get("source")]
    for e in ready:
        if not e.get("rights_confirmed"):
            print(f"{e['id']}: rights_confirmed is false; refusing to ingest it.")
            return 2
    if not ready:
        print("No episodes with a source in golden/episodes.json.")
        return 2

    from clipforge.tools.check_env import load_report

    results = []
    for e in ready:
        print(f"== {e['id']} {e.get('title') or ''}")
        try:
            results.append(run_episode(e, args.n, args.pick_only))
        except Exception as ex:  # record and continue with the next episode
            print(f"  FAILED: {ex}")
            results.append({"id": e["id"], "title": e.get("title"), "error": str(ex)})
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "golden-summary.md").write_text(
        summary_markdown(results, load_report()), encoding="utf-8"
    )
    print(f"Wrote {REPORT_DIR / 'golden-summary.md'}")
    return 1 if any(r.get("error") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
